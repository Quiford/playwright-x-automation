"""
TweeterTweet Replier — Main Orchestrator
Coordinates watcher, scheduler, responder, and dashboard.
Supports pause/resume, active hours, crash recovery, and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import json
import random
import signal
import sys
import time
from pathlib import Path

from config import load_settings
from notifier import TelegramNotifier
from responder import Responder
from scheduler import ScheduleEngine
from storage import StateDB
from utils import setup_logging, get_logger
from watcher import TweetWatcher

logger = get_logger("main")


class Orchestrator:
    """Central async engine that ties all modules together."""

    def __init__(self) -> None:
        self.cfg = load_settings()
        setup_logging(self.cfg.log_level, "engagement.log")
        self.db = StateDB(self.cfg.database_path)
        self.watcher = TweetWatcher(self.cfg)
        self.notifier = TelegramNotifier(
            self.cfg.telegram.bot_token, self.cfg.telegram.chat_id
        )
        self.scheduler = ScheduleEngine(self.cfg, self.db, self.notifier)
        self.responder = Responder(self.cfg, self.db, self.notifier)
        self.accounts: List[str] = []
        self._paused = False
        self._shutdown = asyncio.Event()
        self._start_time = time.time()
        self._reply_lock = asyncio.Lock()

    def _load_accounts(self) -> None:
        path = Path(self.cfg.accounts_json_path)
        if not path.exists():
            logger.warning("Accounts file not found: %s", path)
            self.accounts = []
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = [a["handle"].lstrip("@").lower() for a in data]
            # Filter out junk (hashtags, search URLs, etc.)
            self.accounts = [h for h in raw if h and "?" not in h and "/" not in h and "=" not in h and "#" not in h]
            if len(self.accounts) != len(raw):
                logger.warning("Filtered %d junk entries from accounts", len(raw) - len(self.accounts))
            logger.info("Loaded %d account(s)", len(self.accounts))
        except Exception as exc:
            logger.error("Failed to load accounts: %s", exc)
            self.accounts = []

    async def run(self) -> None:
        self._load_accounts()

        await self.watcher.start()

        # Optional one-shot login helper
        auth_path = Path(self.cfg.browser.user_data_dir) / "auth.json"
        if not auth_path.exists():
            logger.warning("No saved browser state found. Running login helper...")
            await self.watcher.login_and_save_state()

        # Auto-following fallback
        if not self.accounts and self.cfg.my_handle:
            logger.info("accounts.json empty — auto-scraping following list...")
            scraped = await self.watcher.fetch_following_list(
                self.cfg.my_handle, self.cfg.max_accounts
            )
            if scraped:
                self.accounts = [h.lower() for h in scraped]
                # Persist back to JSON so user can edit / inspect
                path = Path(self.cfg.accounts_json_path)
                path.write_text(
                    json.dumps([{"handle": h} for h in self.accounts], indent=2),
                    encoding="utf-8",
                )
                logger.info("Saved %d auto-discovered accounts to %s", len(self.accounts), path)
            else:
                logger.error("Could not scrape following list. Populate data/accounts.json manually.")
                return

        if not self.accounts:
            logger.error("No accounts to monitor. Populate data/accounts.json or set MY_HANDLE in .env.")
            return

        # Telegram startup notification (includes full account list)
        await self.notifier.notify_startup(len(self.accounts), self.accounts)

        # Clear stale pending items and cooldowns for a fresh start.
        # Processed tweets are NOT fully cleared — only records older than 24h are removed
        # so the bot avoids double-replying to recent tweets across restarts.
        self.db.clear_pending()
        self.db.clear_cooldowns()
        self.db.clear_old_processed(hours=24)

        logger.info(
            "Loaded config: reply_probability=%.1f, cooldown=%d min, daily_limit=%d",
            self.cfg.engagement.reply_probability,
            self.cfg.engagement.per_account_cooldown_minutes,
            self.cfg.engagement.daily_reply_limit,
        )

        # Schedule workers (one watch task cycles all accounts sequentially)
        watcher = asyncio.create_task(self._watch_loop())
        heartbeat = asyncio.create_task(self._heartbeat_loop())

        # Signal handlers (Windows fallback for NotImplementedError)
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT,):
                loop.add_signal_handler(sig, self._request_shutdown)
            if hasattr(signal, "SIGTERM"):
                loop.add_signal_handler(signal.SIGTERM, self._request_shutdown)
        except NotImplementedError:
            # Windows Proactor event loop does not support add_signal_handler
            signal.signal(signal.SIGINT, lambda _s, _f: self._request_shutdown())

        try:
            await self._shutdown.wait()
        except asyncio.CancelledError:
            pass
        finally:
            heartbeat.cancel()
            watcher.cancel()
            try:
                await self.watcher.stop()
            except Exception:
                logger.debug("Watcher stop raised an error (already closed)")

            uptime = int(time.time() - self._start_time)
            stats = self.db.get_stats()
            stats["uptime_seconds"] = uptime
            try:
                await self.notifier.notify_shutdown(stats)
            except Exception:
                pass
            try:
                await self.notifier.close()
            except Exception:
                pass
            logger.info("Shutdown complete. State persisted.")

    async def _watch_loop(self) -> None:
        """Loop: scrape Following timeline — one page load replaces all per-account visits."""
        cycle = 0
        last_refresh = time.time()
        REFRESH_INTERVAL = 600  # 10 minutes
        while not self._shutdown.is_set():
            cycle += 1
            try:
                tweets = await asyncio.wait_for(
                    self.watcher.fetch_following_timeline(max_tweets=30),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[CYCLE %d] Timeline fetch timeout — forcing fresh page", cycle)
                try:
                    if self.watcher._page and not self.watcher._page.is_closed():
                        await self.watcher._page.close()
                except Exception:
                    pass
                self.watcher._page = None
                continue
            except Exception as exc:
                logger.error("[CYCLE %d] Timeline fetch error: %s", cycle, exc)
                continue

            scheduled_any = False
            my_handle_lower = self.cfg.my_handle.lower() if self.cfg.my_handle else ""
            for tweet in tweets:
                if self.db.is_processed(tweet.id):
                    continue
                # Never reply to our own tweets
                if my_handle_lower and tweet.account.lower() == my_handle_lower:
                    self.db.mark_processed(tweet.id, tweet.account, tweet.text)
                    logger.debug("Skipping own tweet from @%s", tweet.account)
                    continue
                scheduled_at = await self.scheduler.schedule_reply(
                    tweet.id, tweet.account, tweet.url, tweet.text, force_delay=0
                )
                self.db.mark_processed(tweet.id, tweet.account, tweet.text)
                if scheduled_at:
                    scheduled_any = True

            # Process any due replies every cycle — don't wait for a new tweet
            await self._process_due_replies()

            # Periodic following-list refresh (sequential, safe with one page)
            # Periodic cleanup of old processed tweets (prevent table bloat)
            if cycle % 10 == 0:
                self.db.clear_old_processed(hours=24)

            if time.time() - last_refresh >= REFRESH_INTERVAL:
                logger.info("Refreshing following list for @%s ...", self.cfg.my_handle)
                try:
                    scraped = await asyncio.wait_for(
                        self.watcher.fetch_following_list(self.cfg.my_handle, self.cfg.max_accounts),
                        timeout=120.0,
                    )
                    if scraped:
                        valid = []
                        for h in scraped:
                            h = str(h).lower().strip()
                            if not h or h in {
                                "home", "explore", "notifications", "messages", "i",
                                "settings", "search", "login", "logout", "compose",
                                "intent", "share", self.cfg.my_handle.lower(),
                            }:
                                continue
                            if any(j in h for j in ("search?q=", "hashtag_click", "%23", "/", ".com")):
                                continue
                            if not re.fullmatch(r"[a-z0-9_]+", h):
                                continue
                            valid.append(h)

                        before = set(self.accounts)
                        after = set(valid)
                        new_handles = list(after - before)
                        if new_handles:
                            self.accounts = list(after)
                            logger.info("Following-list refresh: %d new account(s) added", len(new_handles))
                            await self.notifier.notify_new_accounts(new_handles)
                        else:
                            logger.info("Following-list refresh: no changes (%d accounts)", len(self.accounts))
                except Exception as exc:
                    logger.error("Following-list refresh failed: %s", exc)
                last_refresh = time.time()

            # Stealth: randomized pause between full cycles (8-20s) to avoid mechanical rhythm
            await asyncio.sleep(random.uniform(8, 20))

    async def _refresh_following(self) -> None:
        """Re-scrape the user's following list and append any new accounts."""
        if not self.cfg.my_handle:
            return
        try:
            logger.info("Refreshing following list for @%s ...", self.cfg.my_handle)
            scraped = await self.watcher.fetch_following_list(
                self.cfg.my_handle, self.cfg.max_accounts
            )
            if not scraped:
                return
            current = set(self.accounts)
            new = [h.lower() for h in scraped if h.lower() not in current]
            if new:
                # Filter junk entries (must match the same rules used in fetch_following_list)
                import re
                new = [
                    h for h in new
                    if not any(junk in h for junk in ("search?q=", "hashtag_click", "%23", "/", ".com"))
                    and re.fullmatch(r"[a-z0-9_]+", h)
                ]
                if not new:
                    return
                self.accounts.extend(new)
                # Persist back to JSON
                path = Path(self.cfg.accounts_json_path)
                path.write_text(
                    json.dumps([{"handle": h} for h in self.accounts], indent=2),
                    encoding="utf-8",
                )
                logger.info("Added %d new account(s) from refreshed following list: %s",
                    len(new), ", ".join(new))
                await self.notifier.notify_new_accounts(new)
            else:
                logger.info("Following list refresh: no new accounts found.")
        except Exception as exc:
            logger.error("Failed to refresh following list: %s", exc)

    async def _process_due_replies(self) -> None:
        """Process any queued replies whose delay has elapsed (inline, sequential)."""
        page = await self.watcher._ensure_page()
        if page.is_closed():
            logger.warning("Page closed, creating new page for reply processing")
            self.watcher._page = None
            page = await self.watcher._ensure_page()

        async with self._reply_lock:
            items = self.db.get_pending_items(status="pending", max_age_hours=48)
            if not items:
                return

            logger.info("Processing %d due reply item(s)...", len(items))
            for i, item in enumerate(items):
                if self._shutdown.is_set():
                    break

                try:
                    success = await self.responder.process_queue_item(item, page)
                    if success:
                        logger.info(
                            "Reply sent to @%s (tweet %s)", item.account, item.tweet_id
                        )
                    else:
                        logger.error(
                            "Inline reply failed for @%s tweet %s", item.account, item.tweet_id
                        )
                        self.db.set_item_status(item.id, "failed")
                except Exception as exc:
                    logger.error("Unhandled error in inline reply processing: %s", exc)
                    self.db.set_item_status(item.id, "failed")
                    continue

                # Cooldown between back-to-back inline replies
                if i < len(items) - 1:
                    await asyncio.sleep(random.uniform(2, 5))

    async def _heartbeat_loop(self) -> None:
        """Send periodic Telegram heartbeats."""
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=300.0)
            except asyncio.TimeoutError:
                stats = self.db.get_stats()
                await self.notifier.notify_heartbeat(stats)

    def _request_shutdown(self) -> None:
        logger.info("Shutdown signal received...")
        self._shutdown.set()


async def main() -> None:
    orch = Orchestrator()
    await orch.run()


if __name__ == "__main__":
    asyncio.run(main())
