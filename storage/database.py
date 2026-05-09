"""
SQLite persistence layer for all assistant state.
Handles crash recovery via WAL mode and immediate commits.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger("storage")


@dataclass
class QueuedReply:
    id: int
    tweet_id: str
    account: str
    tweet_url: str
    reply_text: str
    scheduled_at: float
    status: str  # pending | sent | failed | skipped


@dataclass
class EngagementLog:
    id: int
    tweet_id: str
    account: str
    action: str
    detail: str
    created_at: float


class StateDB:
    def __init__(self, db_path: str = "./engagement.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_tweets (
                    tweet_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    tweet_text TEXT,
                    detected_at REAL DEFAULT (unixepoch()),
                    processed_at REAL
                );

                CREATE TABLE IF NOT EXISTS reply_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id TEXT UNIQUE NOT NULL,
                    account TEXT NOT NULL,
                    tweet_url TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    scheduled_at REAL NOT NULL,
                    status TEXT DEFAULT 'pending'
                );

                CREATE TABLE IF NOT EXISTS engagement_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tweet_id TEXT,
                    account TEXT,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created_at REAL DEFAULT (unixepoch())
                );

                CREATE TABLE IF NOT EXISTS cooldowns (
                    account TEXT PRIMARY KEY,
                    last_engagement REAL NOT NULL,
                    daily_count INTEGER DEFAULT 0,
                    window_date TEXT DEFAULT (date('now'))
                );

                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL DEFAULT (unixepoch())
                );

                CREATE INDEX IF NOT EXISTS idx_queue_status ON reply_queue(status);
                CREATE INDEX IF NOT EXISTS idx_queue_scheduled ON reply_queue(scheduled_at);
                CREATE INDEX IF NOT EXISTS idx_log_account ON engagement_log(account);
                CREATE INDEX IF NOT EXISTS idx_log_created ON engagement_log(created_at);
                """
            )
            logger.info("Database schema verified (%s)", self.path)

    # --- processed tweets ---

    def is_processed(self, tweet_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_tweets WHERE tweet_id = ?", (tweet_id,)
            ).fetchone()
            return row is not None

    def mark_detected(self, tweet_id: str, account: str, tweet_text: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_tweets (tweet_id, account, tweet_text)
                VALUES (?, ?, ?)
                """,
                (tweet_id, account, tweet_text),
            )

    def mark_processed(self, tweet_id: str, account: str = "", tweet_text: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO processed_tweets (tweet_id, account, tweet_text, processed_at)
                     VALUES (?, ?, ?, unixepoch())
                     ON CONFLICT(tweet_id) DO UPDATE SET processed_at = unixepoch()""",
                (tweet_id, account, tweet_text),
            )

    # --- queue ---

    def queue_reply(
        self,
        tweet_id: str,
        account: str,
        tweet_url: str,
        reply_text: str,
        scheduled_at: float,
    ) -> None:
        with self._conn() as conn:
            # Skip if already queued (pending or sent) to prevent duplicate replies
            row = conn.execute(
                "SELECT 1 FROM reply_queue WHERE tweet_id = ? AND status IN ('pending', 'sent')",
                (tweet_id,),
            ).fetchone()
            if row:
                return
            conn.execute(
                """
                INSERT INTO reply_queue
                (tweet_id, account, tweet_url, reply_text, scheduled_at, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (tweet_id, account, tweet_url, reply_text, scheduled_at),
            )

    def pop_due_item(self, now: float | None = None) -> Optional[QueuedReply]:
        if now is None:
            now = time.time()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id, tweet_id, account, tweet_url, reply_text, scheduled_at, status
                FROM reply_queue
                WHERE status = 'pending' AND scheduled_at <= ?
                ORDER BY scheduled_at ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE reply_queue SET status = 'processing' WHERE id = ?",
                (row["id"],),
            )
            return QueuedReply(
                id=row["id"],
                tweet_id=row["tweet_id"],
                account=row["account"],
                tweet_url=row["tweet_url"],
                reply_text=row["reply_text"],
                scheduled_at=row["scheduled_at"],
                status="processing",
            )

    def set_item_status(self, item_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE reply_queue SET status = ? WHERE id = ?",
                (status, item_id),
            )

    def remove_queue_item(self, item_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM reply_queue WHERE id = ?", (item_id,))

    def get_queue_counts(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM reply_queue GROUP BY status"
            ).fetchall()
            return {row["status"]: row[1] for row in rows}

    def get_pending_items(
        self, status: str = "pending", max_age_hours: int = 48, limit: int = None
    ) -> List[QueuedReply]:
        now = time.time()
        cutoff = now - max_age_hours * 3600
        with self._conn() as conn:
            sql = """SELECT * FROM reply_queue WHERE status = ? AND scheduled_at <= ? AND scheduled_at >= ? ORDER BY scheduled_at ASC"""
            if limit:
                sql += f" LIMIT {int(limit)}"
            rows = conn.execute(sql, (status, now, cutoff)).fetchall()
            return [
                QueuedReply(
                    id=r["id"],
                    tweet_id=r["tweet_id"],
                    account=r["account"],
                    tweet_url=r["tweet_url"],
                    reply_text=r["reply_text"],
                    scheduled_at=r["scheduled_at"],
                    status=r["status"],
                )
                for r in rows
            ]

    # --- cooldowns ---

    def get_cooldown(self, account: str) -> tuple[float, int, str]:
        """Returns (last_engagement, daily_count, window_date)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_engagement, daily_count, window_date FROM cooldowns WHERE account = ?",
                (account,),
            ).fetchone()
            if row is None:
                return (0.0, 0, "")
            return (row["last_engagement"], row["daily_count"], row["window_date"])

    def record_engagement(self, account: str) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT window_date, daily_count FROM cooldowns WHERE account = ?",
                (account,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO cooldowns (account, last_engagement, daily_count, window_date)
                    VALUES (?, unixepoch(), 1, ?)
                    """,
                    (account, today),
                )
            elif existing["window_date"] != today:
                conn.execute(
                    """
                    UPDATE cooldowns
                    SET last_engagement = unixepoch(), daily_count = 1, window_date = ?
                    WHERE account = ?
                    """,
                    (today, account),
                )
            else:
                conn.execute(
                    """
                    UPDATE cooldowns
                    SET last_engagement = unixepoch(), daily_count = daily_count + 1
                    WHERE account = ?
                    """,
                    (account,),
                )

    def clear_cooldowns(self) -> None:
        """Reset all engagement cooldowns (called on startup so restarts are fresh)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM cooldowns")
        logger.info("Cleared engagement cooldowns for fresh start.")

    def clear_old_processed(self, hours: int = 24) -> None:
        """Remove processed tweets older than N hours to prevent table bloat."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM processed_tweets WHERE processed_at < (unixepoch() - ?)",
                (hours * 3600,),
            )
            if cur.rowcount:
                logger.info("Cleaned up %d processed tweet(s) older than %dh", cur.rowcount, hours)

    # --- logging ---

    def log_action(self, tweet_id: str, account: str, action: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO engagement_log (tweet_id, account, action, detail)
                VALUES (?, ?, ?, ?)
                """,
                (tweet_id, account, action, detail),
            )

    def get_recent_logs(self, limit: int = 100) -> List[EngagementLog]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, tweet_id, account, action, detail, created_at
                FROM engagement_log ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                EngagementLog(
                    id=r["id"],
                    tweet_id=r["tweet_id"],
                    account=r["account"],
                    action=r["action"],
                    detail=r["detail"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def get_stats(self) -> dict:
        with self._conn() as conn:
            total_processed = conn.execute(
                "SELECT COUNT(*) FROM processed_tweets"
            ).fetchone()[0]
            total_sent = conn.execute(
                "SELECT COUNT(*) FROM reply_queue WHERE status = 'sent'"
            ).fetchone()[0]
            total_failed = conn.execute(
                "SELECT COUNT(*) FROM reply_queue WHERE status = 'failed'"
            ).fetchone()[0]
            total_skipped = conn.execute(
                "SELECT COUNT(*) FROM reply_queue WHERE status = 'skipped'"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM reply_queue WHERE status = 'pending'"
            ).fetchone()[0]
            return {
                "processed": total_processed,
                "sent": total_sent,
                "failed": total_failed,
                "skipped": total_skipped,
                "pending": pending,
            }

    # --- system state (crash recovery) ---

    def set_state(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, unixepoch())
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=unixepoch()
                """,
                (key, value),
            )

    def clear_pending(self) -> None:
        """Remove all stale pending items from the queue (e.g. from previous runs)."""
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM reply_queue WHERE status='pending'")
            conn.commit()
            deleted = cursor.rowcount
            if deleted:
                logger.info("Cleared %d stale pending item(s) from previous runs", deleted)

    def clear_processed(self) -> None:
        """Remove all processed tweet records so stats reflect only this run."""
        with self._conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM processed_tweets")
            conn.commit()
            deleted = cursor.rowcount
            if deleted:
                logger.info("Cleared %d stale processed tweet(s) from previous runs", deleted)

    def get_state(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default
