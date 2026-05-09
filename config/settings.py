"""
Pydantic-based configuration for the engagement assistant.
All values are loaded from .env first, then fall back to sensible defaults.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TimeRange(BaseSettings):
    """A single active-hours window."""

    start: int = Field(default=9, ge=0, le=23)
    end: int = Field(default=22, ge=0, le=23)


class BrowserConfig(BaseSettings):
    """Playwright browser settings."""

    model_config = SettingsConfigDict(
        env_prefix="BROWSER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    headless: bool = Field(default=False)
    user_data_dir: str = Field(default="./browser_profile")
    viewport_width: int = Field(default=1280)
    viewport_height: int = Field(default=720)
    locale: str = Field(default="en-US")
    timezone: str = Field(default="America/New_York")


class TwitterSelectors(BaseSettings):
    """
    DOM selectors for X/Twitter.
    Update these when X changes their frontend.
    """

    tweet_article: str = Field(default='article[data-testid="tweet"]')
    tweet_text: str = Field(default='[data-testid="tweetText"]')
    user_name_link: str = Field(default='[data-testid="User-Name"] a[role="link"]')
    reply_button: str = Field(default='button[data-testid="reply"]')
    tweet_textarea: str = Field(default='[data-testid="tweetTextarea_0RichTextInputContainer"] div[contenteditable="true"]')
    tweet_button: str = Field(default='button[data-testid="tweetButton"]')
    replying_to_indicator: str = Field(default='[data-testid="socialContext"]')
    retweet_indicator: str = Field(default='[data-testid="socialContext"]')


class EngagementConfig(BaseSettings):
    """Behavioral tuning for replies."""

    model_config = SettingsConfigDict(
        env_prefix="ENGAGEMENT__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    reply_probability: float = Field(default=0.9, ge=0.0, le=1.0)
    min_delay_seconds: int = Field(default=120)
    max_delay_seconds: int = Field(default=300)
    daily_reply_limit: int = Field(default=50, ge=1)
    view_without_reply_probability: float = Field(default=0.15, ge=0.0, le=1.0)
    scroll_before_reply: bool = Field(default=True)
    scroll_after_reply: bool = Field(default=True)
    mutation_enabled: bool = Field(default=True)
    per_account_cooldown_minutes: int = Field(default=30, ge=0)


class ProxyConfig(BaseSettings):
    """Optional HTTP/SOCKS proxy for the browser."""

    model_config = SettingsConfigDict(
        env_prefix="PROXY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False)
    server: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")


class TelegramConfig(BaseSettings):
    """Optional Telegram bot notifications."""

    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(default=False)
    bot_token: str = Field(default="")
    chat_id: str = Field(default="")


class AppSettings(BaseSettings):
    """Root application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # General
    log_level: str = Field(default="INFO")
    database_path: str = Field(default="./engagement.db")
    replies_json_path: str = Field(default="./data/replies.json")
    accounts_json_path: str = Field(default="./data/accounts.json")
    poll_interval_seconds: int = Field(default=5)
    max_accounts: int = Field(default=100)
    my_handle: str = Field(default="")

    # Time windows
    active_hours: List[TimeRange] = Field(default=[TimeRange()])
    inactive_sleep_minutes: int = Field(default=60)

    # Sub-configs
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    selectors: TwitterSelectors = Field(default_factory=TwitterSelectors)
    engagement: EngagementConfig = Field(default_factory=EngagementConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)

    @field_validator("active_hours", mode="before")
    @classmethod
    def parse_active_hours(cls, v):
        if isinstance(v, str):
            windows = json.loads(v)
            return [TimeRange(**w) for w in windows]
        return v


def load_settings() -> AppSettings:
    return AppSettings()
