"""Optional notifications (Discord / Slack / Telegram) and a dead-man-switch ping, standard library only.

Delivery runs on a background thread with a bounded queue, so a slow or dead webhook never blocks the trading
loop. Messages can carry a key: a keyed message is dropped when the same key was sent less than `every`
seconds ago (no notification storms from a condition that persists for hours).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.request
from collections import deque

from . import __version__
from .config import NotifyConfig

log = logging.getLogger(__name__)
PING_INTERVAL = 300.0  # seconds between dead-man-switch pings (notify.healthcheck_url)
# Explicit User-Agent on every request (webhooks and the dead-man-switch ping): Discord, behind Cloudflare,
# answers urllib's default 'Python-urllib/3.x' with HTTP 403 (error code 1010), so no message would arrive.
USER_AGENT = f"GreatestTrader/{__version__} (+https://github.com/ryu93885/GreatestTraderByClaude)"


class Notifier:
    def __init__(self, cfg: NotifyConfig, prefix: str = "[GreatestTrader]", maxsize: int = 500,
                 background: bool = True):
        self.cfg = cfg
        self.prefix = prefix
        self.background = background
        self.recent: deque[str] = deque(maxlen=200)  # accepted messages (daily summary / tests)
        self.failures = 0  # consecutive failed deliveries (any channel)
        self.failed_total = 0
        self._last: dict[str, float] = {}
        self._q: queue.Queue = queue.Queue(maxsize)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_ping = -1e18

    @property
    def enabled(self) -> bool:
        c = self.cfg
        return bool(c.discord_webhook or c.slack_webhook or (c.telegram_bot_token and c.telegram_chat_id))

    # ------------------------------------------------------------------ API
    def send(self, text: str, key: str | None = None, every: float = 0.0) -> bool:
        """Queue a message. With `key`, drop it if the same key was sent less than `every` seconds ago.
        Never blocks and never raises. Returns True when the message was accepted."""
        if key is not None:
            now = time.monotonic()
            last = self._last.get(key)
            if last is not None and now - last < every:
                return False
            self._last[key] = now
        self.recent.append(text)
        if self.enabled:
            self._put(("msg", text))
        return True

    def reset(self, key: str) -> None:
        """Forget when `key` was last sent: the next message with that key goes out at once."""
        self._last.pop(key, None)

    def heartbeat(self) -> None:
        """Called by the engine after every healthy loop: pings notify.healthcheck_url at most every 5 minutes,
        so the external service raises an alarm when the loop stops (dead VPS, hung process, MT5 down)."""
        url = self.cfg.healthcheck_url
        if not url:
            return
        now = time.monotonic()
        if now - self._last_ping < PING_INTERVAL:
            return
        self._last_ping = now
        self._put(("ping", url))

    def flush(self, timeout: float = 10.0) -> None:
        """Wait (at most `timeout` seconds) until queued messages are delivered (call before exiting)."""
        end = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.05)

    # ------------------------------------------------------------------ delivery
    def _put(self, item: tuple[str, str]) -> None:
        if not self.background:
            self._deliver(item)
            return
        self._ensure_thread()
        try:
            self._q.put_nowait(item)
        except queue.Full:
            log.warning("notification queue full, dropped: %s", item[1][:120])

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="notifier", daemon=True)
                self._thread.start()

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            try:
                self._deliver(item)
            except Exception as exc:  # noqa: BLE001 - the worker must survive anything
                log.warning("notification worker error: %s", exc)
            finally:
                self._q.task_done()

    def _deliver(self, item: tuple[str, str]) -> None:
        kind, text = item
        if kind == "ping":
            self._request(text, None)
            return
        msg = f"{self.prefix} {text}"
        c = self.cfg
        if c.discord_webhook:
            self._request(c.discord_webhook, {"content": msg[:1900]})
        if c.slack_webhook:
            self._request(c.slack_webhook, {"text": msg})
        if c.telegram_bot_token and c.telegram_chat_id:
            url = f"https://api.telegram.org/bot{c.telegram_bot_token}/sendMessage"
            self._request(url, {"chat_id": c.telegram_chat_id, "text": msg[:4000]})

    def _request(self, url: str, payload: dict | None) -> None:
        if payload is None:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
        else:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            urllib.request.urlopen(req, timeout=5).read()
            self.failures = 0
        except Exception as e:  # never let notifications break trading
            self.failures += 1
            self.failed_total += 1
            if self.failures <= 3 or self.failures % 50 == 0:
                log.warning("notification failed (%d in a row): %s", self.failures, e)
