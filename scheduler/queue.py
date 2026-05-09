"""
Scheduler and queue logic.
Decides *when* and *whether* to reply based on:
- active hours
- reply probability
- cooldowns
- daily limits
- random delays
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from config.settings import AppSettings, EngagementConfig, TimeRange
from storage.database import StateDB
from utils.logger import get_logger
from utils.mutation import TextMutator

logger = get_logger("scheduler")

# Optional notifier type hint to avoid circular import at runtime
try:
    from notifier.telegram import TelegramNotifier
except Exception:
    TelegramNotifier = None  # type: ignore


@dataclass
class ReplyTemplate:
    text: str
    tags: List[str]


class ReplyPool:
    """Loads and serves from the local JSON reply pool."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.templates: List[ReplyTemplate] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            logger.warning("Reply pool not found at %s — using empty pool", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.templates = [
                ReplyTemplate(text=item["text"], tags=item.get("tags", []))
                for item in data
            ]
            logger.info("Loaded %d reply templates", len(self.templates))
        except Exception as exc:
            logger.error("Failed to load reply pool: %s", exc)

    def pick(self, recent_choices: List[str]) -> Optional[ReplyTemplate]:
        if not self.templates:
            return None
        # De-prioritize recently used templates
        candidates = [t for t in self.templates if t.text not in recent_choices]
        if not candidates:
            candidates = self.templates
        return random.choice(candidates)


class ScheduleEngine:
    """
    Coordinates detection -> scheduling -> cooldown checks.
    """

    def __init__(
        self,
        settings: AppSettings,
        db: StateDB,
        notifier: Optional["TelegramNotifier"] = None,
    ) -> None:
        self.cfg = settings
        self.eng: EngagementConfig = settings.engagement
        self.db = db
        self.notifier = notifier
        self.pool = ReplyPool(settings.replies_json_path)
        self.mutator = TextMutator()
        self._recent_replies: List[str] = []
        self._max_recent_memory = 20

    def is_active_hours(self) -> bool:
        # Empty config means no time restriction (24/7 active)
        if not self.cfg.active_hours:
            return True
        now = time.gmtime()
        hour = now.tm_hour
        for window in self.cfg.active_hours:
            if window.start <= window.end:
                if window.start <= hour <= window.end:
                    return True
            else:
                # overnight window e.g. 22 -> 06
                if hour >= window.start or hour <= window.end:
                    return True
        return False

    def should_reply_to_account(self, account: str) -> tuple[bool, str]:
        # 1. Probability gate
        if random.random() > self.eng.reply_probability:
            return False, "probability_gate"

        # 2. Cooldown
        last_eng, daily_count, window_date = self.db.get_cooldown(account)
        cooldown_sec = self.eng.per_account_cooldown_minutes * 60
        if time.time() - last_eng < cooldown_sec:
            return False, "cooldown"

        # 3. Daily limit per account
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if window_date == today and daily_count >= self.eng.daily_reply_limit:
            return False, "daily_limit"

        return True, "ok"

    def _generate_reply_text(self) -> Optional[str]:
        """Pick a template, mutate it, and track recent memory. Returns None if no templates."""
        template = self.pool.pick(self._recent_replies)
        if template is None:
            return None
        reply_text = template.text
        if self.eng.mutation_enabled:
            reply_text = self.mutator.mutate(reply_text)
        self._recent_replies.append(template.text)
        if len(self._recent_replies) > self._max_recent_memory:
            self._recent_replies.pop(0)
        return reply_text

    def prepare_inline_reply(self, tweet_id: str, account: str, tweet_text: str = "") -> Optional[str]:
        """
        Checks all scheduling rules (hours, probability, cooldown, daily limit)
        and returns reply text if we should reply immediately.
        Returns None if skipped. Does NOT queue anything.
        """
        if not self.is_active_hours():
            logger.info("Outside active hours — skipping @%s", account)
            return None

        should_reply, reason = self.should_reply_to_account(account)
        if not should_reply:
            if reason == "probability_gate" and random.random() < self.eng.view_without_reply_probability:
                self.db.log_action(tweet_id, account, "viewed_only", "probability_gate")
                logger.info("Viewed-only @%s (probability gate)", account)
                return None
            self.db.log_action(tweet_id, account, "skipped", reason)
            logger.info("Skipped @%s (%s)", account, reason)
            return None

        reply_text = self._generate_reply_text()
        if reply_text is None:
            logger.warning("No reply templates available")
            return None
        return reply_text

    async def schedule_reply(self, tweet_id: str, account: str, tweet_url: str, tweet_text: str = "",
                             force_delay: int | None = None) -> Optional[float]:
        """
        Composes a reply, queues it, and returns the scheduled timestamp.
        Returns None if we decide to skip (e.g. no templates, view-only mode).
        Pass force_delay=0 for immediate replies.
        """
        if not self.is_active_hours():
            logger.info("Outside active hours — skipping @%s", account)
            if self.notifier:
                asyncio.create_task(
                    self.notifier.notify_skipped(account, "outside_hours", tweet_url, tweet_text)
                )
            return None

        should_reply, reason = self.should_reply_to_account(account)
        if not should_reply:
            if reason == "probability_gate" and random.random() < self.eng.view_without_reply_probability:
                # View-only pass
                self.db.log_action(tweet_id, account, "viewed_only", "probability_gate")
                logger.info("Viewed-only @%s (probability gate)", account)
                return None
            self.db.log_action(tweet_id, account, "skipped", reason)
            logger.info("Skipped @%s (%s)", account, reason)
            if self.notifier:
                asyncio.create_task(
                    self.notifier.notify_skipped(account, reason, tweet_url, tweet_text)
                )
            return None

        reply_text = self._generate_reply_text()
        if reply_text is None:
            logger.warning("No reply templates available")
            if self.notifier:
                asyncio.create_task(
                    self.notifier.notify_skipped(account, "no_templates", tweet_url, tweet_text)
                )
            return None

        # Random delay 2-5 minutes (or configured range), or force a specific delay
        if force_delay is not None:
            delay = force_delay
        else:
            delay = random.randint(self.eng.min_delay_seconds, self.eng.max_delay_seconds)
        scheduled_at = time.time() + delay

        self.db.queue_reply(tweet_id, account, tweet_url, reply_text, scheduled_at)
        self.db.log_action(tweet_id, account, "queued", f"delay={delay}s")
        logger.info(
            "Queued reply to @%s in %.0fs (tweet %s)", account, delay, tweet_id
        )
        if self.notifier:
            asyncio.create_task(
                self.notifier.notify_queued(account, delay, tweet_url)
            )
        return scheduled_at
