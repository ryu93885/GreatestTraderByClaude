"""Operations helpers for long unattended runs (Windows VPS): logging, single instance, console, versions."""
from __future__ import annotations

import logging
import os
import platform
import sys
from datetime import datetime, timedelta
from pathlib import Path

EXIT_RESTART = 3  # the wrapper (3_run_live.bat) restarts the process
EXIT_STOP = 10  # clean stop / unrecoverable configuration: the wrapper stops


class DailyFileHandler(logging.Handler):
    """logs/<prefix>_YYYY-MM-DD.log: a new file every calendar day, never renamed (on Windows a midnight rename
    fails while another process holds the file, and TimedRotatingFileHandler then loses every later record).
    Files older than `keep_days` are deleted."""

    def __init__(self, log_dir: str | Path, prefix: str, keep_days: int = 800, encoding: str = "utf-8"):
        super().__init__()
        self.dir, self.prefix, self.keep, self.encoding = Path(log_dir), prefix, keep_days, encoding
        self.dir.mkdir(parents=True, exist_ok=True)
        self._day: str | None = None
        self._f = None

    @property
    def current_file(self) -> Path | None:
        return self.dir / f"{self.prefix}_{self._day}.log" if self._day else None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d")
            if day != self._day or self._f is None:
                if self._f is not None:
                    self._f.close()
                    self._f = None
                self._day = day
                self._f = open(self.dir / f"{self.prefix}_{day}.log", "a", encoding=self.encoding)
                self._prune(day)
            self._f.write(self.format(record) + "\n")
            self._f.flush()
        except Exception:  # noqa: BLE001
            if self._f is not None:  # try a fresh file handle next time
                try:
                    self._f.close()
                except OSError:
                    pass
                self._f = None
            self.handleError(record)

    def _prune(self, today: str) -> None:
        limit = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=self.keep)).strftime("%Y-%m-%d")
        for f in self.dir.glob(f"{self.prefix}_*.log"):
            if f.stem[len(self.prefix) + 1:] < limit:
                try:
                    f.unlink()
                except OSError:
                    pass

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None
        super().close()


def setup_logging(log_dir: str | Path, level: str, prefix: str, console_level: str | None = None,
                  keep_days: int = 800) -> DailyFileHandler:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(lvl)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(getattr(logging, str(console_level or level).upper(), lvl))
    fh = DailyFileHandler(log_dir, prefix, keep_days)
    fh.setFormatter(fmt)
    root.handlers[:] = [sh, fh]
    return fh


def acquire_single_instance_lock(state_file: str | Path):
    """Exclusive lock next to the state file. Keep the returned file object open for the life of the process:
    a second engine on the same state (double-clicked .bat, startup shortcut plus scheduler) fails fast
    instead of doubling every order and overwriting the state."""
    path = Path(state_file).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise RuntimeError(f"another GreatestTrader process already uses {Path(state_file).name} "
                           f"(lock file {path}). Stop it first.") from None
    return f


def disable_console_quickedit() -> bool:
    """Windows: a mouse click in the console window (QuickEdit 'Select' mode) blocks every write to stdout,
    i.e. the next log line freezes the whole trading loop until a key is pressed. Switch QuickEdit off."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080))  # clear QUICK_EDIT, set EXTENDED
    except Exception:  # noqa: BLE001 - no console (scheduler / service): nothing to do
        return False


def process_memory_mb() -> float | None:
    """Resident memory of this process in MB (None when unavailable)."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            proc = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(proc, ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize / 2**20
            return None
        with open("/proc/self/statm", "r") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except Exception:  # noqa: BLE001
        return None


def versions() -> dict[str, str]:
    """Versions of Python and the packages the system depends on (start-up log, check, heartbeat)."""
    out = {"python": platform.python_version(), "platform": platform.platform(terse=True)}
    for mod in ("numpy", "pandas", "yaml", "MetaTrader5"):
        try:
            m = __import__(mod)
            out["PyYAML" if mod == "yaml" else mod] = str(getattr(m, "__version__", "?"))
        except ImportError:
            pass
    return out


def format_versions(v: dict[str, str]) -> str:
    return " ".join(f"{k}={val}" for k, val in v.items())
