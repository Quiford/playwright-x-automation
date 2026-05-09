"""
Async Telegram notifier using raw Bot API via aiohttp.
Sends creative, varied status updates about the engagement bot's activity.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import List, Optional

import aiohttp

from utils.logger import get_logger

logger = get_logger("notifier")


@dataclass
class ReplyResult:
    tweet_id: str
    account: str
    tweet_url: str
    reply_text: str
    tweet_text: str
    scheduled_delay: int
    success: bool


class TelegramNotifier:
    """
    Lightweight Telegram notifier. No extra libraries — just aiohttp.
    Messages are randomized from creative templates so they never feel robotic.
    """

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self._session: Optional[aiohttp.ClientSession] = None
        self.logger = get_logger("notifier")
        if self.enabled:
            masked = bot_token[:10] + "..." if len(bot_token) > 10 else bot_token
            self.logger.info("Telegram notifier enabled (token=%s, chat_id=%s)", masked, chat_id)
        else:
            self.logger.warning("Telegram notifier DISABLED — check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        self._last_online_msg = 0.0
        self._online_interval = 3600  # heartbeat every hour

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _send(self, text: str, disable_preview: bool = True) -> bool:
        if not self.enabled:
            return False

        session = await self._get_session()
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        # Telegram API prefers int for group chat IDs, but accepts strings too
        chat_id = self.chat_id
        try:
            if chat_id.lstrip("-").isdigit():
                chat_id = int(chat_id)
        except AttributeError:
            pass

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }

        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                self.logger.error("Telegram API error %s: %s", resp.status, body)
                return False
        except Exception as exc:
            self.logger.error("Telegram send failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    #  Startup / heartbeat
    # ------------------------------------------------------------------

    async def notify_startup(self, account_count: int, accounts: list[str] | None = None) -> None:
        header = random.choice([
            "🚀 <b>Engagement bot is LIVE</b>",
            "⚡ <b>System online</b>",
            "🎯 <b>Bot awakened</b>",
            "🤖 <b>Automation active</b>",
        ])
        body = f"{header}\nMonitoring <code>{account_count}</code> account(s). Ready to strike when they post. 👀"
        await self._send(body)

        if accounts:
            # Send full list as vertical list (split across multiple messages if needed)
            lines = [f"@{h}" for h in accounts]
            current_msg = "<b>Targets:</b>\n"
            for line in lines:
                if len(current_msg) + len(line) + 1 > 3800:  # leave buffer for tags
                    await self._send(current_msg.strip())
                    current_msg = "<b>Targets (cont.):</b>\n"
                current_msg += line + "\n"
            if current_msg.strip():
                await self._send(current_msg.strip())

        self._last_online_msg = time.time()

    async def notify_new_accounts(self, new_accounts: list[str]) -> None:
        if not new_accounts:
            return
        body = (
            f"🆕 <b>New followings detected!</b>\n"
            f"Added <code>{len(new_accounts)}</code> new account(s) to the watch list:\n"
            f"{'\n'.join(f'@{h}' for h in new_accounts)}"
        )
        await self._send(body)

    async def notify_heartbeat(self, stats: dict) -> None:
        now = time.time()
        if now - self._last_online_msg < self._online_interval:
            return
        templates = [
            f"💓 <b>Heartbeat</b>\n"
            f"Processed: <code>{stats.get('processed', 0)}</code> | "
            f"Sent: <code>{stats.get('sent', 0)}</code> | "
            f"Pending: <code>{stats.get('pending', 0)}</code>\n"
            f"Still alive and hungry.",

            f"🩺 <b>Status check</b>\n"
            f"Tweets scanned: <code>{stats.get('processed', 0)}</code>\n"
            f"Replies fired: <code>{stats.get('sent', 0)}</code>\n"
            f"Queue depth: <code>{stats.get('pending', 0)}</code>\n"
            f"All systems nominal.",
        ]
        await self._send(random.choice(templates))
        self._last_online_msg = now

    # ------------------------------------------------------------------
    #  Queued / scheduled
    # ------------------------------------------------------------------

    async def notify_queued(self, account: str, delay: int, tweet_url: str) -> None:
        templates = [
            f"📥 <b>Target acquired</b>\n"
            f"@{account} just posted.\n"
            f"Reply queued for <code>{delay}s</code> — staying natural.\n"
            f"<a href='{tweet_url}'>View tweet</a>",

            f"🎣 <b>Bait spotted</b>\n"
            f"@{account} dropped something.\n"
            f"Waiting <code>{delay}s</code> before biting.\n"
            f"<a href='{tweet_url}'>Peek</a>",

            f"⏳ <b>Patience mode</b>\n"
            f"@{account}'s tweet detected.\n"
            f"Reply locked and loaded. Firing in <code>{delay}s</code>.\n"
            f"<a href='{tweet_url}'>Original</a>",
        ]
        await self._send(random.choice(templates))

    # ------------------------------------------------------------------
    #  Reply sent
    # ------------------------------------------------------------------

    async def notify_reply_sent(self, result: ReplyResult) -> None:
        account = result.account
        reply = result.reply_text
        tweet_url = result.tweet_url
        delay = result.scheduled_delay
        preview = result.tweet_text[:120] + "…" if len(result.tweet_text) > 120 else result.tweet_text

        templates = [
            f"🎯 <b>DIRECT HIT</b>\n"
            f"Landed on @{account}'s post.\n"
            f"💬 <i>'{reply}'</i>\n"
            f"Delayed <code>{delay}s</code> to stay under radar.\n"
            f"Tweet preview: <i>{preview}</i>\n"
            f"<a href='{tweet_url}'>View reply</a>",

            f"🔥 <b>REPLY FIRED</b>\n"
            f"@{account} never saw it coming.\n"
            f"💬 <i>'{reply}'</i>\n"
            f"Execution delay: <code>{delay}s</code>\n"
            f"Original: <i>{preview}</i>\n"
            f"<a href='{tweet_url}'>Open</a>",

            f"⚡ <b>ENGAGEMENT SECURED</b>\n"
            f"Slid into @{account}'s mentions.\n"
            f"💬 <i>'{reply}'</i>\n"
            f"Crafted after <code>{delay}s</code> of careful timing.\n"
            f"They posted: <i>{preview}</i>\n"
            f"<a href='{tweet_url}'>Check it</a>",

            f"🎪 <b>PERFORMANCE DELIVERED</b>\n"
            f"@{account} got a front-row seat.\n"
            f"💬 <i>'{reply}'</i>\n"
            f"Waited <code>{delay}s</code> — looked totally organic.\n"
            f"Their tweet: <i>{preview}</i>\n"
            f"<a href='{tweet_url}'>Link</a>",

            f"🌊 <b>WAVE MADE</b>\n"
            f"Caught @{account}'s post mid-air.\n"
            f"💬 <i>'{reply}'</i>\n"
            f"Strategic <code>{delay}s</code> delay executed.\n"
            f"Context: <i>{preview}</i>\n"
            f"<a href='{tweet_url}'>See reply</a>",
        ]
        await self._send(random.choice(templates))

    # ------------------------------------------------------------------
    #  Skipped / missed
    # ------------------------------------------------------------------

    async def notify_skipped(self, account: str, reason: str, tweet_url: str, tweet_text: str) -> None:
        preview = tweet_text[:120] + "…" if len(tweet_text) > 120 else tweet_text
        emoji_map = {
            "cooldown": "🧊",
            "daily_limit": "🛑",
            "probability_gate": "🎲",
            "outside_hours": "🌙",
            "no_templates": "📭",
            "error": "⚠️",
        }
        emoji = emoji_map.get(reason, "🚫")
        reason_pretty = reason.replace("_", " ").title()

        templates = [
            f"{emoji} <b>Skipped @{account}</b>\n"
            f"Reason: <code>{reason_pretty}</code>\n"
            f"Tweet: <i>{preview}</i>\n"
            f"<a href='{tweet_url}'>View tweet</a>\n"
            f"Not every shot gets taken. Next one will.",

            f"{emoji} <b>Missed opportunity on @{account}</b>\n"
            f"Why: <code>{reason_pretty}</code>\n"
            f"Their post: <i>{preview}</i>\n"
            f"<a href='{tweet_url}'>Original</a>\n"
            f"Sometimes the best move is no move.",

            f"{emoji} <b>Passed on @{account}</b>\n"
            f"Filter: <code>{reason_pretty}</code>\n"
            f"Content: <i>{preview}</i>\n"
            f"<a href='{tweet_url}'>Link</a>\n"
            f"Smart skips keep the account healthy.",
        ]
        await self._send(random.choice(templates))

    # ------------------------------------------------------------------
    #  Errors / shutdown
    # ------------------------------------------------------------------

    async def notify_error(self, context: str, exception: str) -> None:
        await self._send(
            f"🚨 <b>Error encountered</b>\n"
            f"Where: <code>{context}</code>\n"
            f"What: <pre>{exception[:400]}</pre>\n"
            f"Bot is retrying. Stand by."
        )

    async def notify_shutdown(self, stats: dict) -> None:
        uptime = stats.get("uptime_seconds", 0)
        minutes = int(uptime // 60)
        templates = [
            f"🛑 <b>Bot powered down</b>\n"
            f"Uptime: <code>{minutes}m</code>\n"
            f"Processed: <code>{stats.get('processed', 0)}</code> | "
            f"Sent: <code>{stats.get('sent', 0)}</code> | "
            f"Skipped: <code>{stats.get('skipped', 0)}</code>\n"
            f"See you on the next run. 👋",

            f"🔌 <b>Disconnected</b>\n"
            f"Ran for <code>{minutes}m</code>.\n"
            f"Replies fired: <code>{stats.get('sent', 0)}</code>\n"
            f"Targets scanned: <code>{stats.get('processed', 0)}</code>\n"
            f"Mission complete. Recharging...",
        ]
        await self._send(random.choice(templates))

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
