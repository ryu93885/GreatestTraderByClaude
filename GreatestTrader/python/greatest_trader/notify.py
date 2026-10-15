"""Optional notifications (Discord / Slack / Telegram) using only the standard library."""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from .config import NotifyConfig

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: NotifyConfig, prefix: str = "[GreatestTrader]"):
        self.cfg = cfg
        self.prefix = prefix

    @property
    def enabled(self) -> bool:
        c = self.cfg
        return bool(c.discord_webhook or c.slack_webhook or (c.telegram_bot_token and c.telegram_chat_id))

    def _post(self, url: str, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:  # never let notifications break trading
            log.warning("notification failed: %s", e)

    def send(self, text: str) -> None:
        msg = f"{self.prefix} {text}"
        c = self.cfg
        if c.discord_webhook:
            self._post(c.discord_webhook, {"content": msg[:1900]})
        if c.slack_webhook:
            self._post(c.slack_webhook, {"text": msg})
        if c.telegram_bot_token and c.telegram_chat_id:
            url = f"https://api.telegram.org/bot{c.telegram_bot_token}/sendMessage"
            self._post(url, {"chat_id": c.telegram_chat_id, "text": msg})
