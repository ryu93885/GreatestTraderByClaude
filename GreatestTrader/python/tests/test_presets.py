"""Shipped presets: they load, validate, and the MT5 .set files carry the same values."""
from pathlib import Path

import pytest

from greatest_trader.config import available_presets, load_config, load_preset

ROOT = Path(__file__).resolve().parents[2]
SET_DIR = ROOT / "mql5" / "Presets"
SET_NAMES = {"stable5": "Stable5_Default", "stable4": "Stable4_Robust", "safe2": "Safe2_WorstMonth2pct",
             "classic_conservative": "Classic_Conservative", "classic_standard": "Classic_Standard",
             "classic_aggressive": "Classic_Aggressive"}
EA_KEYS = {"risk_nr": ("InpRiskNR", 100), "risk_es": ("InpRiskES", 100), "max_total_risk": ("InpMaxTotalRisk", 100),
           "max_same_direction_per_currency": ("InpMaxSameDirPerCcy", 1),
           "monthly_hard_stop": ("InpMonthlyHardStop", 100), "monthly_loss_limit": ("InpMonthlyLossLimit", 100),
           "dd_scale_full": ("InpDDScaleFull", 100), "dd_scale_floor": ("InpDDScaleFloor", 1),
           "daily_loss_limit": ("InpDailyLossLimit", 100)}


def read_set(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-16").splitlines():
        if line and not line.startswith(";") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_default_config_uses_stable5():
    cfg = load_config(ROOT / "python" / "config" / "config.yaml")
    assert cfg.preset == "stable5" and abs(cfg.risk.risk_nr - 0.035) < 1e-12


@pytest.mark.parametrize("name", sorted(SET_NAMES))
def test_preset_loads_and_matches_mt5_set(name):
    assert name in available_presets()
    cfg = load_config(None, preset=name)
    raw = load_preset(name)["risk"]
    st = read_set(SET_DIR / f"GreatestTrader_{SET_NAMES[name]}.set")
    for key, (ea_key, scale) in EA_KEYS.items():
        if key in raw:
            assert abs(float(st[ea_key]) - raw[key] * scale) < 1e-9, (name, key)
    mult = dict(kv.split(":") for kv in st["InpSymbolRiskMult"].split(",") if kv)
    assert {k: float(v) for k, v in mult.items()} == {k: float(v) for k, v in cfg.risk.symbol_risk_mult.items()}
    assert st["InpBlackoutStart"] == cfg.risk.blackout_start and st["InpBlackoutEnd"] == cfg.risk.blackout_end


EA_PROFILE = {"conservative": "0", "standard": "1", "aggressive": "2", "custom": "3"}
EA_ALL = {"max_open_positions": ("InpMaxOpen", 1), "max_positions_per_currency": ("InpMaxPerCurrency", 1),
          "dd_scale_full": ("InpDDScaleFull", 100), "dd_scale_floor": ("InpDDScaleFloor", 1),
          "daily_loss_limit": ("InpDailyLossLimit", 100), "max_drawdown_halt": ("InpMaxDDHalt", 100),
          "max_total_risk": ("InpMaxTotalRisk", 100), "max_same_direction_per_currency": ("InpMaxSameDirPerCcy", 1),
          "daily_hard_stop": ("InpDailyHardStop", 100), "monthly_loss_limit": ("InpMonthlyLossLimit", 100),
          "monthly_hard_stop": ("InpMonthlyHardStop", 100), "vol_target_daily": ("InpVolTargetDaily", 100),
          "vol_lookback_days": ("InpVolLookbackDays", 1), "vol_mult_min": ("InpVolMultMin", 1),
          "vol_mult_max": ("InpVolMultMax", 1), "eq_filter_days": ("InpEqFilterDays", 1),
          "eq_filter_mult": ("InpEqFilterMult", 1)}


@pytest.mark.parametrize("name", sorted(SET_NAMES))
def test_mt5_set_equals_resolved_python_risk_config(name):
    """Every risk input of the .set file equals the fully resolved Python RiskConfig of the preset."""
    r = load_config(None, preset=name).risk
    st = read_set(SET_DIR / f"GreatestTrader_{SET_NAMES[name]}.set")
    assert st["InpRiskProfile"] == EA_PROFILE[r.profile], name
    if r.profile == "custom":
        assert abs(float(st["InpRiskNR"]) - r.risk_nr * 100) < 1e-9
        assert abs(float(st["InpRiskES"]) - r.risk_es * 100) < 1e-9
    for key, (ea_key, scale) in EA_ALL.items():
        assert abs(float(st[ea_key]) - getattr(r, key) * scale) < 1e-9, (name, key)
    assert (st["InpUseMinLot"] == "true") == (r.min_lot_policy == "min")


def test_ea_default_inputs_equal_stable5_preset():
    import re
    src = (ROOT / "mql5" / "Experts" / "GreatestTrader.mq5").read_text()
    defaults = {m.group(1): m.group(2).strip().strip('"')
                for m in re.finditer(r"^input\s+\w+\s+(Inp\w+)\s*=\s*([^;]+);", src, re.M)}
    st = read_set(SET_DIR / "GreatestTrader_Stable5_Default.set")
    for key in ["InpRiskNR", "InpRiskES", "InpMaxTotalRisk", "InpMaxSameDirPerCcy", "InpMonthlyHardStop",
                "InpDDScaleFull", "InpDDScaleFloor", "InpDailyLossLimit", "InpSymbolRiskMult", "InpBlackoutStart",
                "InpBlackoutEnd", "InpBBPeriod", "InpBBDev", "InpATRCostMult", "InpSLATRMult", "InpMaxHoldBars"]:
        a, b = defaults[key], st[key]
        try:
            assert abs(float(a) - float(b)) < 1e-9, key
        except ValueError:
            assert a == b, key
    assert defaults["InpRiskProfile"] == "GT_RISK_CUSTOM" and st["InpRiskProfile"] == "3"
