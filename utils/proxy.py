"""
Proxy configuration builder for Playwright.
"""

from __future__ import annotations

from typing import Any

from config.settings import ProxyConfig


class ProxyRotator:
    """
    Returns a Playwright-compatible proxy dict when enabled.
    Supports single-proxy mode (no rotation) since this is a
    local personal-automation tool.
    """

    def __init__(self, cfg: ProxyConfig) -> None:
        self.cfg = cfg

    def get_playwright_proxy(self) -> dict[str, Any] | None:
        if not self.cfg.enabled or not self.cfg.server:
            return None
        proxy: dict[str, Any] = {"server": self.cfg.server}
        if self.cfg.username:
            proxy["username"] = self.cfg.username
        if self.cfg.password:
            proxy["password"] = self.cfg.password
        return proxy
