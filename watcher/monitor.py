"""
Playwright-based tweet watcher.
Navigates target profiles, scrapes top-level tweets, and yields clean tweet dicts.
Resilient to DOM changes via configurable selectors.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, BrowserContext, Page
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config.settings import AppSettings, BrowserConfig, TwitterSelectors
from utils.logger import get_logger

logger = get_logger("watcher")


@dataclass
class Tweet:
    id: str
    account: str
    text: str
    url: str
    is_top_level: bool = True
    created_at: float | None = None  # unix timestamp from snowflake ID


class TweetWatcher:
    def __init__(self, settings: AppSettings) -> None:
        self.cfg = settings
        self.browser_cfg: BrowserConfig = settings.browser
        self.sel: TwitterSelectors = settings.selectors
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._playwright = None
        self._start_time: float | None = None

    async def start(self) -> None:
        logger.info("Launching Playwright browser...")
        self._start_time = time.time()
        self._playwright = await async_playwright().start()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
        ]

        proxy = self.cfg.proxy.get_playwright_proxy() if hasattr(self.cfg.proxy, "get_playwright_proxy") else None
        # ProxyRotator is in utils, not on config directly. Fix below.

        from utils.proxy import ProxyRotator
        proxy_cfg = ProxyRotator(self.cfg.proxy).get_playwright_proxy()

        browser = await self._playwright.chromium.launch(
            headless=self.browser_cfg.headless,
            args=args,
            proxy=proxy_cfg,
        )

        context_kwargs = {
            "viewport": {
                "width": self.browser_cfg.viewport_width,
                "height": self.browser_cfg.viewport_height,
            },
            "locale": self.browser_cfg.locale,
            "timezone_id": self.browser_cfg.timezone,
        }

        user_data = self.browser_cfg.user_data_dir
        if user_data and self._state_exists(user_data):
            context_kwargs["storage_state"] = f"{user_data}/auth.json"

        self._context = await browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()

        # Anti-automation mitigation
        await self._page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            """
        )

        logger.info("Browser ready (headless=%s)", self.browser_cfg.headless)

    def _state_exists(self, user_data_dir: str) -> bool:
        from pathlib import Path
        return Path(user_data_dir, "auth.json").exists()

    async def stop(self) -> None:
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        logger.info("Browser stopped.")

    async def login_and_save_state(self) -> None:
        """
        Manual one-shot login helper.
        Opens X, waits for the user to log in manually, then saves state.
        """
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        logger.info("Navigating to X login page...")
        await self._page.goto("https://x.com/login")
        logger.info("Please log in manually in the browser window.")
        await self._page.wait_for_url("https://x.com/home", timeout=300_000)
        logger.info("Login detected. Saving state...")
        await self._save_state()
        logger.info("State saved to %s/auth.json", self.browser_cfg.user_data_dir)

    async def _save_state(self) -> None:
        from pathlib import Path
        Path(self.browser_cfg.user_data_dir).mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=f"{self.browser_cfg.user_data_dir}/auth.json")

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(3),
    )
    async def _ensure_page(self) -> Page:
        """Return the current page, re-initializing if needed."""
        if self._page is None or self._page.is_closed():
            if self._context is None:
                await self.start()
            else:
                self._page = await self._context.new_page()
        return self._page

    @retry(
        retry=retry_if_exception_type(Exception),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(3),
    )
    async def fetch_account_tweets(self, account: str, max_tweets: int = 20,
                                   counter: int | None = None, total: int | None = None) -> List[Tweet]:
        """Scrape top-level tweets for a given handle (only fresh ones from last 6h)."""
        url = f"https://x.com/{account.lstrip('@')}"
        extra = f" ---> {counter}/{total}" if counter and total else ""
        logger.debug("[trace] fetch_account_tweets start @%s%s", account, extra)

        page = await self._ensure_page()
        # Recover if page was closed/crashed
        if page.is_closed():
            logger.warning("Page closed, creating new page for @%s", account)
            self._page = None
            page = await self._ensure_page()

        logger.debug("[trace] navigating to %s ...", url)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.error("[trace] page.goto failed for @%s: %s", account, exc)
            return []
        logger.debug("[trace] page.goto done for @%s", account)

        # Let the page settle so X finishes its lazy hydration
        await asyncio.sleep(2.5)
        logger.debug("[trace] hydration sleep done for @%s", account)

        # Wait for at least one article to appear (X SPA can be slow)
        try:
            await page.wait_for_selector("article[data-testid='tweet']", timeout=25_000)
            logger.debug("[trace] selector found for @%s", account)
        except Exception:
            logger.warning("No tweets found for @%s (timeout)%s", account, extra)
            return []

        # Parse tweets
        tweets: List[Tweet] = []
        seen = set()
        articles = await page.query_selector_all("article[data-testid='tweet']")
        now = time.time()
        # Only process tweets posted after the bot started (with 30s buffer)
        fresh_cutoff = self._start_time - 30 if self._start_time else now - 60

        for article in articles[:max_tweets]:
            link_el = await article.query_selector("a[href*='/status/']")
            if not link_el:
                continue
            href = await link_el.get_attribute("href")
            if not href:
                continue
            expected_prefix = f"/{account.lstrip('@')}/status/"
            if not href.lower().startswith(expected_prefix.lower()):
                continue
            tweet_id = href.split("/status/")[-1].split("?")[0]
            if tweet_id in seen or not tweet_id:
                continue
            seen.add(tweet_id)

            # Extract timestamp from snowflake ID
            created_at = self._snowflake_to_timestamp(tweet_id)
            if created_at and created_at < fresh_cutoff:
                logger.debug("Skipping pre-boot tweet %s (posted before bot started)", tweet_id)
                continue

            text_el = await article.query_selector("div[data-testid='tweetText']")
            text = await text_el.inner_text() if text_el else ""
            tweet_url = f"https://x.com{href}"
            tweets.append(
                Tweet(
                    id=tweet_id,
                    account=account.lstrip("@").lower(),
                    text=text,
                    url=tweet_url,
                    created_at=created_at,
                )
            )
        extra = f" ---> {counter}/{total}" if counter and total else ""
        logger.info("Fetched %d fresh top-level tweet(s) from @%s%s", len(tweets), account, extra)
        return tweets

    async def fetch_following_timeline(self, max_tweets: int = 30) -> List[Tweet]:
        """Scrape tweets from the Following timeline on x.com/home (no per-account visits)."""
        logger.debug("[trace] fetch_following_timeline start")

        page = await self._ensure_page()
        if page.is_closed():
            logger.warning("Page closed, creating new page for timeline")
            self._page = None
            page = await self._ensure_page()

        logger.debug("[trace] navigating to https://x.com/home ...")
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            logger.error("[trace] Timeline page.goto failed: %s", exc)
            return []
        logger.debug("[trace] page.goto done for timeline")

        await asyncio.sleep(2.5)
        logger.debug("[trace] hydration sleep done for timeline")

        # Click the "Following" tab (it's a <div role="tab">, not an <a>)
        try:
            clicked = await page.evaluate("""() => {
                const tabs = document.querySelectorAll('[role="tab"]');
                for (const t of tabs) {
                    if (t.innerText.trim() === 'Following' && t.getAttribute('aria-selected') !== 'true') {
                        t.click();
                        return true;
                    }
                }
                return false;
            }""")
            if clicked:
                logger.debug("Clicked Following tab")
                await asyncio.sleep(2.0)
            else:
                logger.debug("Following tab already active or not found")
        except Exception as exc:
            logger.warning("Following tab click failed: %s", exc)

        articles = await page.query_selector_all("article[data-testid='tweet']")
        now = time.time()
        fresh_cutoff = self._start_time - 30 if self._start_time else now - 60
        logger.debug("Timeline: found %d raw article(s), fresh_cutoff=%s", len(articles), time.strftime("%H:%M:%S", time.localtime(fresh_cutoff)))

        tweets: List[Tweet] = []
        seen: set[str] = set()

        for article in articles[:max_tweets]:
            # Get the main tweet status link (first /status/ link in the article)
            link_el = await article.query_selector("a[href*='/status/']")
            if not link_el:
                logger.debug("Skipping article: no status link")
                continue
            href = await link_el.get_attribute("href")
            if not href:
                logger.debug("Skipping article: empty href")
                continue

            # Normalize href: handle both /handle/status/ID and https://x.com/handle/status/ID
            if href.startswith("https://x.com"):
                href = href[len("https://x.com"):]
            elif href.startswith("http://x.com"):
                href = href[len("http://x.com"):]
            elif not href.startswith("/"):
                logger.debug("Skipping article: unexpected href format %r", href)
                continue

            # Parse href: /handle/status/12345
            parts = href.strip("/").split("/")
            if len(parts) < 3 or parts[1] != "status":
                logger.debug("Skipping article: unexpected href parts %s", parts)
                continue

            account = parts[0].lower()
            tweet_id = parts[2].split("?")[0]
            if not tweet_id or tweet_id in seen:
                continue
            seen.add(tweet_id)

            # Extract timestamp from snowflake ID
            created_at = self._snowflake_to_timestamp(tweet_id)
            if created_at and created_at < fresh_cutoff:
                logger.debug("Skipping pre-boot tweet %s from @%s (created_at=%s < cutoff=%s)", tweet_id, account, time.strftime("%H:%M:%S", time.localtime(created_at)) if created_at else "None", time.strftime("%H:%M:%S", time.localtime(fresh_cutoff)))
                continue

            # Skip retweets (has socialContext with "Retweeted" or "reposted")
            social_ctx = await article.query_selector('[data-testid="socialContext"]')
            if social_ctx:
                ctx_text = await social_ctx.inner_text()
                if ctx_text and ("retweet" in ctx_text.lower() or "reposted" in ctx_text.lower()):
                    logger.debug("Skipping retweet %s from @%s", tweet_id, account)
                    continue

            # Skip promoted / ads
            if await article.query_selector('[data-testid="app-text"]'):
                logger.debug("Skipping promoted tweet %s from @%s", tweet_id, account)
                continue

            text_el = await article.query_selector("div[data-testid='tweetText']")
            text = await text_el.inner_text() if text_el else ""
            tweet_url = f"https://x.com/{account}/status/{tweet_id}"
            tweets.append(
                Tweet(
                    id=tweet_id,
                    account=account,
                    text=text,
                    url=tweet_url,
                    created_at=created_at,
                )
            )
            logger.debug("Accepted fresh tweet %s from @%s", tweet_id, account)

        logger.info("Fetched %d fresh tweet(s) from Following timeline (out of %d raw articles)", len(tweets), len(articles))
        return tweets

    async def fetch_following_list(self, my_handle: str, max_accounts: int = 1000) -> List[str]:
        """
        Scrapes the authenticated user's following list from X/Twitter.
        Only extracts handles from cells whose button says "Following" or "Unfollow",
        which guarantees they are real followings and not "Who to follow" suggestions.
        """
        if not self._page or self._page.is_closed():
            await self._ensure_page()
        page = self._page
        assert page is not None

        handle = my_handle
        url = f"https://x.com/{handle}/following"
        logger.info("Navigating to %s ...", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        current_url = page.url
        if "login" in current_url or "i/flow" in current_url:
            logger.warning("Redirected to login page (%s) — session may be expired.", current_url)
            return []

        title = await page.title()
        logger.info("Page loaded: url=%s title=%s", current_url, title)

        following: list[str] = []
        last_count = 0
        stagnant_loops = 0
        skip = {"home", "explore", "notifications", "messages", "i", "settings",
                "search", "login", "logout", "compose", "intent", "share", handle.lower()}

        while len(following) < max_accounts and stagnant_loops < 5:
            # Run JS inside browser: find cells with Following/Unfollow button, extract handle
            new_handles = await page.evaluate("""(skipList) => {
                const handles = new Set();
                // Scope to timeline region to avoid sidebar suggestions
                const timeline = document.querySelector('[aria-label*="Timeline"]') || document.body;
                const cells = timeline.querySelectorAll('[data-testid="cellInnerDiv"], [data-testid="UserCell"], [data-testid="userCell"]');

                for (const cell of cells) {
                    const buttons = cell.querySelectorAll('button');
                    let isReal = false;
                    for (const btn of buttons) {
                        const text = (btn.textContent || '').trim().toLowerCase();
                        const aria = (btn.getAttribute('aria-label') || '').trim().toLowerCase();
                        // Real following: button says "Following" or "Unfollow"
                        if (text.includes('following') || text.includes('unfollow') ||
                            aria.includes('following') || aria.includes('unfollow')) {
                            isReal = true;
                            break;
                        }
                        // Suggestion: button says "Follow" (not "Following")
                        if (text === 'follow' ||
                            (text.includes('follow') && !text.includes('following'))) {
                            isReal = false;
                            break;
                        }
                        if (aria.includes('follow') && !aria.includes('following') && !aria.includes('unfollow')) {
                            isReal = false;
                            break;
                        }
                    }
                    if (!isReal) continue;

                    // Extract handle from first simple profile link in the cell
                    const links = cell.querySelectorAll('a[href^="/"]');
                    for (const link of links) {
                        const href = link.getAttribute('href') || '';
                        const m = href.match(/^\/([a-zA-Z0-9_]+)\/?$/);
                        if (m) {
                            const h = m[1].toLowerCase();
                            if (!skipList.includes(h)) {
                                handles.add(h);
                            }
                            break;
                        }
                    }
                }
                return Array.from(handles);
            }""", list(skip))

            added = 0
            for raw in new_handles:
                h = str(raw).lower().strip()
                if h in skip:
                    continue
                if any(j in h for j in ("search?q=", "hashtag_click", "%23", "/", ".com")):
                    continue
                if not re.fullmatch(r"[a-z0-9_]+", h):
                    continue
                if h not in following:
                    following.append(h)
                    added += 1
                    if len(following) >= max_accounts:
                        break

            logger.debug("Scroll pass: added %d new (total %d)", added, len(following))

            if len(following) == last_count:
                stagnant_loops += 1
            else:
                stagnant_loops = 0
                last_count = len(following)

            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
            await asyncio.sleep(random.uniform(1.5, 2.5))

        logger.info("Scraped %d followings for @%s", len(following), handle)
        return following

    @staticmethod
    def _snowflake_to_timestamp(tweet_id: str) -> float | None:
        """Derive Unix timestamp from Twitter/X snowflake ID."""
        try:
            snowflake = int(tweet_id)
            # Twitter epoch: 1288834974657 ms
            ts_ms = (snowflake >> 22) + 1288834974657
            return ts_ms / 1000.0
        except (ValueError, OverflowError):
            return None

    async def _parse_article(self, article, expected_handle: str) -> Optional[Tweet]:
        try:
            # Extract tweet link to derive ID
            link_el = await article.query_selector("a[href*='status/']")
            if not link_el:
                return None
            href = await link_el.get_attribute("href")
            if not href or "/status/" not in href:
                return None
            tweet_id = href.split("/status/")[-1].split("/")[0].split("?")[0]
            tweet_url = f"https://x.com{href.split('?')[0]}"

            # Extract text
            text = ""
            text_el = await article.query_selector(self.sel.tweet_text)
            if text_el:
                text = await text_el.inner_text()

            # Determine author
            author_handle = expected_handle
            user_link = await article.query_selector(self.sel.user_name_link)
            if user_link:
                href_author = await user_link.get_attribute("href")
                if href_author:
                    author_handle = href_author.strip("/").split("/")[-1]

            # Top-level filters
            is_top_level = True

            # 1. Must be authored by the expected account
            if author_handle.lower() != expected_handle.lstrip("@").lower():
                is_top_level = False

            # 2. Check for "Replying to" or repost indicators
            social = await article.query_selector(self.sel.replying_to_indicator)
            if social:
                social_text = await social.inner_text()
                if "replying to" in social_text.lower() or "reposted" in social_text.lower():
                    is_top_level = False

            # 3. Exclude if text contains quote-tweet url pattern ( heuristic )
            if "https://t.co/" in text and text.count("https://t.co/") >= 2:
                is_top_level = False

            return Tweet(
                id=tweet_id,
                account=author_handle,
                text=text[:500],
                url=tweet_url,
                is_top_level=is_top_level,
            )
        except Exception as exc:
            logger.debug("Parse article error: %s", exc)
            return None

    async def random_idle_behaviour(self) -> None:
        """Simulate human-like pauses before/after actions."""
        if self._page is None:
            return
        # Small scroll
        if random.random() < 0.5:
            await self._page.evaluate(
                f"window.scrollBy(0, {random.randint(200, 600)})"
            )
        await asyncio.sleep(random.uniform(1.0, 3.0))
