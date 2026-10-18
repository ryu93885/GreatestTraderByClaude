"""Configuration (YAML) with defaults and validation."""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Something in config.yaml (or the chosen preset) is wrong: fix the file, a restart cannot help."""


DEFAULT_NR_SYMBOLS = [
    "EURJPY", "GBPJPY", "USDJPY", "GBPUSD", "EURGBP", "USDCHF", "EURUSD",
    "GBPCAD", "EURCAD", "AUDCAD", "GBPCHF", "EURAUD", "CHFJPY", "GBPAUD",
]

# Risk presets: fraction of equity risked per trade (distance entry -> stop loss).
RISK_PRESETS = {
    "conservative": {"nr": 0.005, "es": 0.005},
    "standard": {"nr": 0.010, "es": 0.010},
    "aggressive": {"nr": 0.015, "es": 0.015},
}

# Typical round-trip cost in pips (spread + commission) used ONLY by the Python
# backtester when the data has no spread column. Live trading uses real spreads.
DEFAULT_BACKTEST_SPREADS = {
    "EURUSD": 1.0, "GBPUSD": 1.5, "USDJPY": 1.1, "USDCHF": 1.6, "EURGBP": 1.5,
    "EURJPY": 1.7, "GBPJPY": 2.6, "GBPCAD": 3.0, "EURCAD": 2.2, "AUDCAD": 2.0,
    "GBPCHF": 2.6, "EURAUD": 2.2, "CHFJPY": 2.4, "GBPAUD": 3.0, "AUDUSD": 1.3,
    "USDCAD": 1.6, "NZDUSD": 1.8, "AUDJPY": 1.8, "CADJPY": 2.0, "EURNZD": 3.0,
    "GBPNZD": 4.0, "CADCHF": 2.2, "EURCHF": 1.8, "AUDNZD": 2.2, "NZDJPY": 2.0,
}


@dataclass
class AccountConfig:
    login: int | None = None
    password: str | None = None
    server: str | None = None
    terminal_path: str | None = None


@dataclass
class BrokerConfig:
    symbol_suffix: str = ""
    symbol_map: dict[str, str] = field(default_factory=dict)  # e.g. {"EURUSD": "EURUSD.raw"}
    server_time_mode: str = "auto"  # auto | ny7 | fixed
    server_gmt_offset: float = 2.0  # used when server_time_mode == fixed
    commission_per_lot: float = 0.0  # round-trip commission per 1.0 lot, ACCOUNT currency
    deviation_points: int = 20


@dataclass
class NightReversionConfig:
    enabled: bool = True
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_NR_SYMBOLS))
    bb_period: int = 15
    bb_deviation: float = 1.8
    atr_period: int = 14
    atr_cost_mult: float = 2.0  # ATR(pips) must be >= this x (spread + commission + slippage)
    sl_atr_mult: float = 3.0
    max_hold_bars: int = 12  # M15 bars (3 hours)
    entry_hours: list[int] = field(default_factory=lambda: [0, 1])  # market-time hours of signal bar
    skip_monday: bool = True
    slippage_pips: float = 0.5
    max_spread_pips: float = 5.0
    max_band_z: float = 0.0  # skip when |close - mid| > this x stdev (news spike); 0 = off
    max_bar_range_atr: float = 0.0  # skip when signal-bar range > this x ATR (news spike); 0 = off
    magic: int = 7101


@dataclass
class EuroSessionConfig:
    enabled: bool = True
    symbol: str = "EURUSD"
    direction: int = -1  # -1 = sell EUR
    entry_utc_hour: int = 7
    exit_utc_hour: int = 13
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # UTC weekday, Mon=0
    atr_period_h1: int = 24
    sl_atr_mult: float = 3.0
    max_spread_pips: float = 2.0
    slippage_pips: float = 0.3
    magic: int = 7102


@dataclass
class RiskConfig:
    profile: str = "standard"  # conservative | standard | aggressive | custom
    risk_nr: float = 0.010  # used when profile == custom
    risk_es: float = 0.010
    max_open_positions: int = 10
    max_positions_per_currency: int = 4
    dd_scale_full: float = 0.30  # risk multiplier = max(floor, 1 - DD / dd_scale_full)
    dd_scale_floor: float = 0.30
    daily_loss_limit: float = 0.05  # stop new entries for the day after -5% (from day start equity)
    max_drawdown_halt: float = 0.40  # stop ALL new entries when DD from peak >= 40%
    min_lot_policy: str = "skip"  # skip | min  (when calculated lot < broker minimum)
    # --- additional portfolio guards (0 / empty = off)
    max_total_risk: float = 0.0  # sum of open initial risk (fraction of equity) incl. the new trade
    max_same_direction_per_currency: int = 0  # e.g. 2 -> at most 2 positions that are long JPY
    daily_hard_stop: float = 0.0  # close everything and stop for the day after this loss (MTM)
    monthly_loss_limit: float = 0.0  # no new entries for the rest of the month after this loss (MTM)
    monthly_hard_stop: float = 0.0  # close everything and stop for the month after this loss (MTM)
    symbol_risk_mult: dict[str, float] = field(default_factory=dict)  # e.g. {"EURUSD": 0.5}
    # --- adaptive risk (0 = off)
    vol_target_daily: float = 0.0  # target daily equity volatility, e.g. 0.01 = 1%/day
    vol_lookback_days: int = 20
    vol_mult_min: float = 0.25
    vol_mult_max: float = 1.5
    eq_filter_days: int = 0  # equity-curve filter: SMA length in days
    eq_filter_mult: float = 0.5  # risk multiplier while equity < its SMA
    # --- year-end holiday blackout: no new entries between these market dates (MM-DD, inclusive;
    #     empty = off). Thin year-end liquidity produced the worst flash-crash fills (e.g. 2019-01-03).
    blackout_start: str = "12-20"
    blackout_end: str = "01-05"
    # --- capacity caps (0 = off). The strategy enters right after the NY 17:00 rollover, when the market is
    #     thinnest, so the size of one order cannot grow with equity forever: above the cap, profit grows in
    #     currency, not in % per month. Applied BEFORE the portfolio guards (the total-risk cap sees the
    #     capped amount); the broker's volume_max applies as well (the tighter wins).
    max_lots_per_order: float = 20.0  # lots of one order, every strategy
    max_risk_amount: float = 0.0  # money at risk of one trade (entry -> stop), ACCOUNT currency

    def base_risk(self, strategy: str) -> float:
        if self.profile == "custom":
            return {"nr": self.risk_nr, "es": self.risk_es}[strategy]
        if self.profile not in RISK_PRESETS:
            raise ValueError(f"unknown risk profile: {self.profile}")
        return RISK_PRESETS[self.profile][strategy]


@dataclass
class NotifyConfig:
    discord_webhook: str = ""
    slack_webhook: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    healthcheck_url: str = ""  # dead-man switch (e.g. healthchecks.io): pinged every 5 minutes while running


@dataclass
class MonitorConfig:
    """Notification-only monitoring: never changes trading."""
    dd_alert: float = 0.15  # notify when drawdown from peak >= this (at most once a day while above; 0 = off)
    daily_summary: bool = True  # summary at every market-day change
    edge_window_trades: int = 250  # R-based edge monitor: last N closed trades of the journal, at each month end
    stale_feed_minutes: float = 10.0  # notify (hourly) when server time stops for this long on a market weekday


@dataclass
class EngineConfig:
    dry_run: bool = True  # True: compute & log signals but send no orders
    poll_seconds: float = 5.0
    state_file: str = "state/state.json"  # dry-run uses <name>_dryrun.json next to it
    log_dir: str = "logs"
    journal_dir: str = "journal"  # append-only trade journal and daily equity log
    log_level: str = "INFO"
    history_bars: int = 300


@dataclass
class BacktestConfig:
    data_dir: str = "data"
    start: str | None = None
    end: str | None = None
    initial_equity: float = 10000.0
    account_currency: str = "USD"  # lots and the capacity caps are converted with the loaded pairs (USD, JPY, ...)
    contract_size: float = 100000.0  # units per 1.0 lot
    leverage: float = 0.0  # free-margin check like live (skip when margin > 80% of free margin); 0 = off
    spreads_pips: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BACKTEST_SPREADS))
    commission_pips: float = 0.0  # extra round-trip commission in pips (if spreads exclude it)
    output_dir: str = "backtest_output"


@dataclass
class Config:
    account: AccountConfig = field(default_factory=AccountConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    night_reversion: NightReversionConfig = field(default_factory=NightReversionConfig)
    euro_session: EuroSessionConfig = field(default_factory=EuroSessionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    preset: str = ""

    def preset_label(self) -> str:
        """Preset name for logs/reports, plus the risk keys that config.yaml overrides on top of it."""
        name = self.preset or "-"
        ov = getattr(self, "_risk_overrides", [])
        return f"{name} + config.yaml risk overrides ({', '.join(ov)})" if self.preset and ov else name

    def broker_symbol(self, base: str) -> str:
        return self.broker.symbol_map.get(base, base + self.broker.symbol_suffix)

    def validate(self) -> None:
        r = self.risk
        if r.profile not in (*RISK_PRESETS, "custom"):
            raise ValueError(f"risk.profile must be one of {list(RISK_PRESETS) + ['custom']}")
        for name in ("nr", "es"):
            v = r.base_risk(name)
            if not 0 <= v <= 0.05:
                raise ValueError(f"risk per trade for {name} must be between 0 and 5% (got {v})")
        if r.max_open_positions < 1:
            raise ValueError("risk.max_open_positions must be >= 1")
        if r.symbol_risk_mult is None:  # an empty `symbol_risk_mult:` in YAML
            r.symbol_risk_mult = {}
        if not isinstance(r.symbol_risk_mult, dict):
            raise ValueError("risk.symbol_risk_mult must be a mapping such as {EURJPY: 0.25}")
        mults = {}
        for s, m in r.symbol_risk_mult.items():
            if isinstance(m, bool) or not isinstance(m, (int, float)) or m < 0:
                raise ValueError(f"risk.symbol_risk_mult[{s}] must be a number >= 0 (got {m!r})")
            for name in ("nr", "es"):
                if r.base_risk(name) * m > 0.05 + 1e-12:
                    raise ValueError(f"risk per trade for {s} ({name}: base risk x symbol_risk_mult) must be <= 5%")
            mults[str(s).strip().upper()[:6]] = float(m)  # base name, like the EA (a broker suffix is ignored)
        r.symbol_risk_mult = mults
        for k in ("max_total_risk", "daily_loss_limit", "daily_hard_stop", "monthly_loss_limit", "monthly_hard_stop",
                  "max_drawdown_halt", "dd_scale_full", "dd_scale_floor", "vol_target_daily", "eq_filter_mult"):
            v = getattr(r, k)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise ValueError(f"risk.{k} must be a fraction between 0 and 1 (got {v!r})")
        for k in ("max_same_direction_per_currency", "vol_lookback_days", "eq_filter_days"):
            v = getattr(r, k)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"risk.{k} must be an integer >= 0 (got {v!r})")
        if isinstance(r.max_positions_per_currency, bool) or not isinstance(r.max_positions_per_currency, int) \
                or r.max_positions_per_currency < 1:
            raise ValueError("risk.max_positions_per_currency must be an integer >= 1")
        if not (_is_num(r.vol_mult_min) and _is_num(r.vol_mult_max) and 0 <= r.vol_mult_min <= r.vol_mult_max):
            raise ValueError("risk: need 0 <= vol_mult_min <= vol_mult_max")
        for v in (r.blackout_start, r.blackout_end):
            if v and not _valid_mmdd(v):
                raise ValueError(f"risk.blackout_start/end must be MM-DD (got {v!r})")
        for k in ("max_lots_per_order", "max_risk_amount"):
            v = getattr(r, k)
            if not _is_num(v) or v < 0:
                raise ValueError(f"risk.{k} must be a number >= 0 (0 = off, got {v!r})")
        m = self.monitor
        if not _is_num(m.dd_alert) or not 0 <= m.dd_alert <= 1:
            raise ValueError(f"monitor.dd_alert must be a fraction between 0 and 1 (got {m.dd_alert!r})")
        if isinstance(m.edge_window_trades, bool) or not isinstance(m.edge_window_trades, int) \
                or m.edge_window_trades < 1:
            raise ValueError("monitor.edge_window_trades must be an integer >= 1")
        if not _is_num(m.stale_feed_minutes) or m.stale_feed_minutes <= 0:
            raise ValueError("monitor.stale_feed_minutes must be > 0")
        if not _is_num(self.engine.poll_seconds) or not 1 <= self.engine.poll_seconds <= 20:
            # bar signals are acted on within 150 s of the close and clock observations need ticks read < 30 s apart
            raise ValueError(f"engine.poll_seconds must be between 1 and 20 (got {self.engine.poll_seconds!r})")
        b = self.backtest
        if not _is_num(b.leverage) or b.leverage < 0 or not _is_num(b.contract_size) or b.contract_size <= 0:
            raise ValueError("backtest.leverage must be >= 0 and backtest.contract_size > 0")
        if not isinstance(b.account_currency, str) or len(b.account_currency.strip()) != 3:
            raise ValueError(f"backtest.account_currency must be a currency code such as USD or JPY "
                             f"(got {b.account_currency!r})")
        b.account_currency = b.account_currency.strip().upper()
        if bool(r.blackout_start) != bool(r.blackout_end):
            raise ValueError("risk.blackout_start and blackout_end must both be set or both empty")
        bk = self.broker
        if bk.server_time_mode not in ("auto", "ny7", "fixed"):
            raise ValueError("broker.server_time_mode must be auto, ny7 or fixed")
        if not _is_num(bk.server_gmt_offset) or not -12 <= bk.server_gmt_offset <= 14:
            raise ValueError(f"broker.server_gmt_offset must be a number of hours between -12 and 14, e.g. 2 or 2.0 "
                             f"(got {bk.server_gmt_offset!r})")
        if bk.symbol_suffix is None:  # an empty `symbol_suffix:` in YAML
            bk.symbol_suffix = ""
        if not isinstance(bk.symbol_suffix, str):
            raise ValueError(f"broker.symbol_suffix must be text such as \".raw\" (got {bk.symbol_suffix!r})")
        if bk.symbol_map is None:  # an empty `symbol_map:` in YAML
            bk.symbol_map = {}
        if not isinstance(bk.symbol_map, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                                          for k, v in bk.symbol_map.items()):
            raise ValueError(f"broker.symbol_map must be a mapping such as {{EURUSD: EURUSDmicro}} "
                             f"(got {bk.symbol_map!r})")
        # keys are pair names like the symbols lists below (looked up with the same normalized name)
        bk.symbol_map = {_symbol(k, "broker.symbol_map"): v.strip() for k, v in bk.symbol_map.items()}
        nr = self.night_reversion
        if not isinstance(nr.symbols, (list, tuple)):  # `symbols: GBPCAD` would be split into G, B, P, ...
            raise ValueError(f"night_reversion.symbols must be a list such as [GBPCAD, EURCAD] (one pair: [GBPCAD]), "
                             f"got {nr.symbols!r}")
        nr.symbols = [_symbol(s, "night_reversion.symbols") for s in nr.symbols]
        if nr.enabled and not nr.symbols:
            raise ValueError("night_reversion.symbols is empty: list at least one pair, or set "
                             "night_reversion.enabled: false")
        if nr.bb_period < 2 or nr.atr_period < 2:
            raise ValueError("indicator periods must be >= 2")
        nr.entry_hours = _int_list(nr.entry_hours, "night_reversion.entry_hours", 0, 23)
        if not (_is_num(nr.max_band_z) and _is_num(nr.max_bar_range_atr)) or nr.max_band_z < 0 \
                or nr.max_bar_range_atr < 0:
            raise ValueError("night_reversion.max_band_z / max_bar_range_atr must be >= 0 (0 = off)")
        es = self.euro_session
        es.symbol = _symbol(es.symbol, "euro_session.symbol")
        es.weekdays = _int_list(es.weekdays, "euro_session.weekdays", 0, 6)
        if not (0 <= es.entry_utc_hour < es.exit_utc_hour <= 23):
            raise ValueError("euro_session: need 0 <= entry_utc_hour < exit_utc_hour <= 23")
        if es.direction not in (-1, 1):
            raise ValueError("euro_session.direction must be -1 (sell) or 1 (buy)")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_SYMBOL = re.compile(r"[A-Za-z]{6}[^\s,;]*")


def _symbol(v: Any, key: str) -> str:
    """A pair's base name: text that starts with the 6 letters of the two currencies (EURUSD). The 6 letters are
    upper-cased (the broker's name comes from broker.symbol_suffix / symbol_map)."""
    if not isinstance(v, str) or not _SYMBOL.fullmatch(v.strip()):
        raise ValueError(f"{key}: each pair must be a name of 6 letters such as EURUSD (got {v!r})")
    v = v.strip()
    return v[:6].upper() + v[6:]


def _int_list(v: Any, key: str, lo: int, hi: int) -> list[int]:
    if not isinstance(v, (list, tuple)) or not all(isinstance(x, int) and not isinstance(x, bool) and lo <= x <= hi
                                                   for x in v):
        raise ValueError(f"{key} must be a list of whole numbers {lo}..{hi} such as [{lo}, {lo + 1}] (got {v!r})")
    return list(v)


def _valid_mmdd(v: str) -> bool:
    try:
        m, d = (int(x) for x in str(v).split("-"))
    except ValueError:
        return False
    return 1 <= m <= 12 and 1 <= d <= 31


def _merge(dc_obj: Any, data: dict, path: str) -> Any:
    names = {f.name: f for f in dataclasses.fields(dc_obj)}
    for k, v in (data or {}).items():
        if k not in names:
            raise KeyError(f"unknown config key: {path}{k}")
        cur = getattr(dc_obj, k)
        if dataclasses.is_dataclass(cur):
            _merge(cur, v, f"{path}{k}.")
        else:
            setattr(dc_obj, k, v)
    return dc_obj


PRESET_DIR = Path(__file__).resolve().parent / "presets"


def available_presets() -> list[str]:
    return sorted(p.stem for p in PRESET_DIR.glob("*.yaml"))


def load_preset(name: str) -> dict:
    """Return the raw override dict of a bundled preset (greatest_trader/presets/<name>.yaml)."""
    p = PRESET_DIR / f"{name}.yaml"
    if not p.exists():
        raise ValueError(f"unknown preset '{name}'. Available: {', '.join(available_presets())}")
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: str | Path | None, preset: str | None = None) -> Config:
    """Load config.yaml. If it contains `preset: <name>` (or `preset` is given), the bundled preset
    is applied first and the user's settings are merged on top of it."""
    cfg = Config()
    data: dict = {}
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    name = preset or data.pop("preset", None)
    data.pop("preset", None)
    if name:
        _merge(cfg, load_preset(name), "preset:")
    _merge(cfg, data, "")
    cfg.preset = name or ""
    cfg._risk_overrides = sorted((data.get("risk") or {}).keys())
    ignored = [k for k in ("risk_nr", "risk_es") if k in cfg._risk_overrides]
    if ignored and cfg.risk.profile != "custom":
        raise ConfigError(f"risk.{' / risk.'.join(ignored)} only apply with risk.profile: custom, but the profile is "
                          f"'{cfg.risk.profile}'" + (f" (preset {name})" if name else "") + ": they would be ignored. "
                          "Use risk.profile: conservative / standard / aggressive, or set risk.profile: custom "
                          "together with both risk_nr and risk_es")
    cfg.validate()
    return cfg
