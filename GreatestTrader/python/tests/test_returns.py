"""Monthly / annual return calculation (regression tests for the verified definitions)."""
import numpy as np
import pandas as pd

from greatest_trader.backtest import BacktestResult
from greatest_trader.report import annual_table, save_report, yearly_table


def _result(points, start=None, end=None, initial=10000.0):
    idx = pd.DatetimeIndex([t for t, _ in points])
    eq = pd.Series([v for _, v in points], index=idx, name="equity")
    n = len(points)
    trades = pd.DataFrame({
        "strategy": ["NR"] * n, "symbol": ["EURJPY"] * n, "entry_time": idx - pd.Timedelta(hours=1),
        "exit_time": idx - pd.Timedelta(minutes=15), "R": np.ones(n), "pnl": np.diff([initial] + list(eq.values)),
    })
    return BacktestResult(trades, eq, initial, period_start=start, period_end=end)


def test_first_month_is_included():
    # three +10% months, the first trade closes on Jan 2 (regression: Jan used to be dropped)
    r = _result([("2020-01-02 10:30", 11000.0), ("2020-02-03 10:30", 12100.0), ("2020-03-02 10:30", 13310.0)],
                start=pd.Timestamp("2020-01-01"), end=pd.Timestamp("2020-03-31 21:45"))
    m = r.monthly_returns()
    assert list(m.index.strftime("%Y-%m")) == ["2020-01", "2020-02", "2020-03"]
    assert np.allclose(m.values, [0.10, 0.10, 0.10])


def test_month_without_trades_is_zero_and_identities_hold():
    r = _result([("2019-11-05", 10500.0), ("2020-01-10", 9975.0), ("2020-03-10", 10972.5)],
                start=pd.Timestamp("2019-11-01"), end=pd.Timestamp("2020-03-31 21:45"))
    m = r.monthly_returns()
    assert list(m.index.strftime("%Y-%m")) == ["2019-11", "2019-12", "2020-01", "2020-02", "2020-03"]
    assert np.allclose(m.values, [0.05, 0.0, -0.05, 0.0, 0.10])
    final_over_initial = r.equity.iloc[-1] / r.initial_equity
    assert np.isclose(np.prod(1 + m.values), final_over_initial)
    a = r.annual_returns()
    assert np.isclose(a.loc[2019, "return"], 0.05)
    assert np.isclose(a.loc[2020, "return"], 10972.5 / 10500.0 - 1)
    assert np.isclose(np.prod(1 + a["return"].values), final_over_initial)
    # annual return == compounding the months of that year
    for y, grp in m.groupby(m.index.year):
        assert np.isclose(np.prod(1 + grp.values) - 1, a.loc[y, "return"])
    yt = yearly_table(r)
    assert np.isclose(yt.loc[2019, "年間"], 5.0) and np.isnan(yt.loc[2019, "1月"])


def test_partial_months_detected():
    r = _result([("2007-01-02 10:30", 10100.0), ("2022-03-04 15:00", 20000.0)],
                start=pd.Timestamp("2007-01-01"), end=pd.Timestamp("2022-03-04 21:45"))
    parts = [str(p) for p in r.partial_months()]
    assert parts == ["2022-03"]  # Jan 1 2007 start covers the whole month, data stops on Mar 4 2022
    a = r.annual_returns()
    assert bool(a.loc[2022, "partial"]) and not bool(a.loc[2007, "partial"])
    st = r.stats()
    assert st["months"] == 183 and st["complete_months"] == 182
    assert "部分期間" in annual_table(r).loc[2022, "備考"]


def test_cagr_and_compound_monthly():
    r = _result([("2020-06-30 12:00", 12100.0), ("2021-12-31 12:00", 14641.0)],
                start=pd.Timestamp("2020-01-01"), end=pd.Timestamp("2021-12-31 21:45"))
    st = r.stats()
    years = (pd.Timestamp("2021-12-31 21:45") - pd.Timestamp("2020-01-01")).total_seconds() / (365.25 * 86400)
    assert np.isclose(st["CAGR"], 1.4641 ** (1 / years) - 1)
    assert np.isclose(st["monthly_compound"], (1 + st["CAGR"]) ** (1 / 12) - 1)


def test_empty_result_does_not_crash(tmp_path):
    r = BacktestResult(pd.DataFrame(), pd.Series(dtype=float), 10000.0)
    assert r.stats() == {"trades": 0}
    assert len(r.monthly_returns()) == 0
    assert yearly_table(r).empty
    save_report(r, tmp_path)
