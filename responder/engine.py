"""
Playwright-based reply poster.
Simulates natural typing, scrolling, and pauses.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Optional

from playwright.async_api import Page

from config.settings import AppSettings, EngagementConfig, TwitterSelectors
from storage.database import StateDB, QueuedReply
from utils.logger import get_logger

logger = get_logger("responder")

# Optional notifier import to avoid circular deps
try:
    from notifier.telegram import TelegramNotifier, ReplyResult
except Exception:
    TelegramNotifier = None  # type: ignore
    ReplyResult = None  # type: ignore


class Responder:
    def __init__(
        self,
        settings: AppSettings,
        db: StateDB,
        notifier: Optional["TelegramNotifier"] = None,
    ) -> None:
        self.cfg = settings
        self.eng: EngagementConfig = settings.engagement
        self.sel: TwitterSelectors = settings.selectors
        self.db = db
        self.notifier = notifier

    async def process_queue_item(self, item: QueuedReply, page: Page) -> bool:
        """
        Executes a single queued reply via Playwright.
        Returns True on success, False on failure.
        """
        success = False
        try:
            success = await self._send_reply(item, page)
        except Exception as exc:
            logger.exception("Unhandled error sending reply to %s", item.tweet_id)
            self.db.log_action(item.tweet_id, item.account, "error", str(exc))

        if success:
            self.db.set_item_status(item.id, "sent")
            self.db.mark_processed(item.tweet_id)
            self.db.record_engagement(item.account)
            self.db.log_action(item.tweet_id, item.account, "sent", item.reply_text[:100])
            logger.info("Reply sent to @%s (tweet %s)", item.account, item.tweet_id)
            if self.notifier and ReplyResult:
                asyncio.create_task(
                    self.notifier.notify_reply_sent(
                        ReplyResult(
                            tweet_id=item.tweet_id,
                            account=item.account,
                            tweet_url=item.tweet_url,
                            reply_text=item.reply_text,
                            tweet_text="",
                            scheduled_delay=int(item.scheduled_at - time.time()),
                            success=True,
                        )
                    )
                )
        else:
            self.db.set_item_status(item.id, "failed")
            self.db.log_action(item.tweet_id, item.account, "failed", "")
            if self.notifier:
                asyncio.create_task(
                    self.notifier.notify_error(
                        f"reply_to_{item.tweet_id}",
                        f"Failed to reply to @{item.account} tweet {item.tweet_id}",
                    )
                )

        return success

    async def _send_reply(self, item: QueuedReply, page: Page) -> bool:
        # Navigate to tweet
        logger.debug("Navigating to %s", item.tweet_url)
        await page.goto(item.tweet_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Optional scroll to simulate reading
        if self.eng.scroll_before_reply:
            await page.evaluate(f"window.scrollBy(0, {random.randint(100, 400)})")
            await asyncio.sleep(random.uniform(1.0, 2.5))

        # Click reply button
        reply_btn = await page.query_selector(self.sel.reply_button)
        if not reply_btn:
            logger.warning("Reply button not found for %s", item.tweet_url)
            return False
        await self._human_click(page, reply_btn)
        await asyncio.sleep(random.uniform(1.0, 2.0))

        # Focus textarea
        textarea = await page.query_selector(self.sel.tweet_textarea)
        if not textarea:
            logger.warning("Textarea not found for %s", item.tweet_url)
            return False
        await self._human_click(page, textarea)
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # Type with human-like delay
        await self._human_type(page, item.reply_text)
        await asyncio.sleep(random.uniform(0.8, 1.8))

        # Submit
        tweet_btn = await page.query_selector(self.sel.tweet_button)
        if not tweet_btn:
            logger.warning("Tweet submit button not found")
            return False
        await self._human_click(page, tweet_btn)
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Check for error toast (heuristic)
        error_toast = await page.query_selector("[data-testid='toast']")
        if error_toast:
            error_text = await error_toast.inner_text()
            if "error" in error_text.lower() or "failed" in error_text.lower():
                logger.warning("X returned error toast: %s", error_text)
                return False

        # Optional scroll after
        if self.eng.scroll_after_reply:
            await page.evaluate(f"window.scrollBy(0, {random.randint(200, 600)})")
            await asyncio.sleep(random.uniform(1.0, 2.0))

        return True

    async def _human_click(self, page: Page, element) -> None:
        """Move cursor to element center with slight random offset, then click."""
        box = await element.bounding_box()
        if box:
            x = box["x"] + box["width"] / 2 + random.uniform(-5, 5)
            y = box["y"] + box["height"] / 2 + random.uniform(-5, 5)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.1, 0.3))
        await element.click()

    async def _human_type(self, page: Page, text: str) -> None:
        """Type text with randomized per-character delays.

        Uses keyboard.type for simple ASCII and insert_text for emojis / unicode
        so Playwright doesn't crash on unknown keys like '🙌'.
        """
        for char in text:
            delay = random.uniform(0.03, 0.12)
            if ord(char) < 128 and char.isprintable():
                await page.keyboard.type(char, delay=delay * 1000)
            else:
                # Emojis and non-ASCII need insert_text (no keypress events)
                await page.keyboard.insert_text(char)
                await asyncio.sleep(delay)
            # occasional longer pause
            if random.random() < 0.05:
                await asyncio.sleep(random.uniform(0.2, 0.6))
