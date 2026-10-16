"""Notifier (non-blocking, rate limited, dead-man switch) and operations helpers (logging, lock, console)."""
import logging
import os
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import greatest_trader.notify as notify_mod
from greatest_trader.config import NotifyConfig
from greatest_trader.notify import Notifier
from greatest_trader.ops import (DailyFileHandler, acquire_single_instance_lock, disable_console_quickedit,
                                 process_memory_mb, versions)


def refused_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens: connection refused at once
    return f"http://127.0.0.1:{port}/hook"


def test_notifier_never_blocks_the_loop():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    threading.Thread(target=lambda: [srv.accept() for _ in range(16)], daemon=True).start()  # accepts, never answers
    url = f"http://127.0.0.1:{srv.getsockname()[1]}/hook"
    n = Notifier(NotifyConfig(discord_webhook=url, slack_webhook=url))
    t0 = time.monotonic()
    for i in range(5):
        n.send(f"CLOSE {i}")
    assert time.monotonic() - t0 < 0.5  # v1.1: 2 dead webhooks blocked 10 s per message


def test_notifier_rate_limits_by_key_and_keeps_recent():
    n = Notifier(NotifyConfig())  # no channel: nothing is posted, messages are still recorded
    assert n.send("ERROR: x", key="err", every=60) and not n.send("ERROR: x", key="err", every=60)
    assert n.send("plain") and n.send("plain")
    assert list(n.recent) == ["ERROR: x", "plain", "plain"]


def test_notifier_counts_failures_and_flushes():
    n = Notifier(NotifyConfig(discord_webhook=refused_url()))
    n.send("a")
    n.send("b")
    n.flush(10)
    assert n.failures == 2 and n.failed_total == 2
    s = Notifier(NotifyConfig(slack_webhook=refused_url()), background=False)  # synchronous mode
    s.send("c")
    assert s.failed_total == 1


def test_healthcheck_ping_every_five_minutes(monkeypatch):
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    t = [1000.0]
    monkeypatch.setattr(notify_mod.time, "monotonic", lambda: t[0])
    n = Notifier(NotifyConfig(healthcheck_url=f"http://127.0.0.1:{srv.server_address[1]}/ping/abc"))
    n.heartbeat()
    t[0] += 10
    n.heartbeat()  # too early: no second ping
    t[0] += 300
    n.heartbeat()
    end = time.time() + 10
    while len(hits) < 2 and time.time() < end:
        time.sleep(0.02)
    srv.shutdown()
    assert hits == ["/ping/abc", "/ping/abc"] and n.failed_total == 0
    Notifier(NotifyConfig()).heartbeat()  # no URL: nothing happens


def test_daily_file_handler_never_renames_and_prunes(tmp_path):
    h = DailyFileHandler(tmp_path, "live", keep_days=3)
    lg = logging.getLogger("dfh-test")
    lg.handlers[:] = [h]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    (tmp_path / "live_2000-01-01.log").write_text("old\n")
    (tmp_path / "backtest_2000-01-01.log").write_text("another command\n")
    lg.info("hello")
    today = tmp_path / f"live_{datetime.now():%Y-%m-%d}.log"
    assert sorted(tmp_path.glob("live_*.log")) == [today] and "hello" in today.read_text()
    assert (tmp_path / "backtest_2000-01-01.log").exists()  # other commands' files are not touched
    rec = lg.makeRecord("dfh-test", logging.INFO, __file__, 1, "tomorrow", None, None)
    rec.created = time.time() + 86400
    h.handle(rec)  # the day changes: a new file, the old one is neither renamed nor lost
    assert today.exists() and len(list(tmp_path.glob("live_*.log"))) == 2
    h.close()
    lg.handlers[:] = []


def test_single_instance_lock(tmp_path):
    a = acquire_single_instance_lock(tmp_path / "state.json")
    with pytest.raises(RuntimeError, match="already uses state.json"):
        acquire_single_instance_lock(tmp_path / "state.json")
    b = acquire_single_instance_lock(tmp_path / "state_dryrun.json")  # dry-run has its own state and lock
    a.close()
    b.close()
    acquire_single_instance_lock(tmp_path / "state.json").close()


def test_console_and_process_info():
    if os.name != "nt":
        assert disable_console_quickedit() is False
    mem = process_memory_mb()
    assert mem is None or mem > 1
    v = versions()
    assert {"python", "numpy", "pandas", "PyYAML"} <= set(v)
