"""Configuration (YAML) with defaults and validation."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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


@dataclass
class EngineConfig:
    dry_run: bool = True  # True: compute & log signals but send no orders
    poll_seconds: float = 5.0
    state_file: str = "state/state.json"
    log_dir: str = "logs"
    log_level: str = "INFO"
    history_bars: int = 300


@dataclass
class BacktestConfig:
    data_dir: str = "data"
    start: str | None = None
    end: str | None = None
    initial_equity: float = 10000.0
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
    engine: EngineConfig = field(default_factory=EngineConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    preset: str = ""

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
        if self.broker.server_time_mode not in ("auto", "ny7", "fixed"):
            raise ValueError("broker.server_time_mode must be auto, ny7 or fixed")
        nr = self.night_reversion
        if nr.bb_period < 2 or nr.atr_period < 2:
            raise ValueError("indicator periods must be >= 2")
        if any(h < 0 or h > 23 for h in nr.entry_hours):
            raise ValueError("night_reversion.entry_hours must be 0..23")
        es = self.euro_session
        if not (0 <= es.entry_utc_hour < es.exit_utc_hour <= 23):
            raise ValueError("euro_session: need 0 <= entry_utc_hour < exit_utc_hour <= 23")
        if es.direction not in (-1, 1):
            raise ValueError("euro_session.direction must be -1 (sell) or 1 (buy)")


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
    cfg.validate()
    return cfg
