"""Operations fixes of review round 2: reset-halt wrapper, webhook User-Agent, config read retry and type checks,
backtest clock warning."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from greatest_trader import __version__
from greatest_trader.config import NotifyConfig
from greatest_trader.notify import USER_AGENT, Notifier

PY = Path(__file__).resolve().parents[1]


def test_reset_halt_wrapper_rebases_the_peak():
    """D8: the documented recovery after a max-DD halt must resume trading, like the EA's InpResetHalt (always
    rebases): without --rebase-peak the halt is set again at once while DD >= max_drawdown_halt."""
    bat = (PY / "7_reset_halt.bat").read_bytes().decode("ascii")
    assert "\r\n" in bat and "\n" not in bat.replace("\r\n", "")  # CRLF only, for cmd.exe
    lines = bat.split("\r\n")
    cmd = [ln for ln in lines if ln.startswith("python ")]
    assert cmd == ["python main.py reset-halt --rebase-peak -c config\\config.yaml"]
    echo = " ".join(ln for ln in lines if ln.lower().startswith("echo "))
    assert "resets the peak equity to the current account" in echo and "equity" in echo
    assert "0%%" in echo  # a single % would be eaten by cmd.exe
    assert 'if /i not "%ANSWER%"=="y" goto end' in lines


def test_every_webhook_and_ping_request_sends_an_explicit_user_agent():
    """py-clock-ops#2: urllib's default 'Python-urllib/3.x' is refused by Discord / Cloudflare (HTTP 403)."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def _ok(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            seen.append((self.command, self.path, self.headers.get("User-Agent")))
            self.send_response(200)
            self.end_headers()

        do_GET = do_POST = _ok

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        n = Notifier(NotifyConfig(discord_webhook=f"{base}/discord", slack_webhook=f"{base}/slack",
                                  healthcheck_url=f"{base}/ping"), background=False)
        n.send("HALT: test")
        n.heartbeat()
    finally:
        srv.shutdown()
    assert USER_AGENT == f"GreatestTrader/{__version__} (+https://github.com/ryu93885/GreatestTraderByClaude)"
    assert sorted(seen) == [("GET", "/ping", USER_AGENT), ("POST", "/discord", USER_AGENT),
                            ("POST", "/slack", USER_AGENT)]
    assert n.failed_total == 0
