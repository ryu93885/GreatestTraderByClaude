"""Backtest reporting: text summary, CSV files and (optional) charts."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .backtest import BacktestResult

LABELS = {
    "period": "期間", "trades": "取引回数", "trades_per_month": "月平均取引回数", "win_rate": "勝率",
    "avg_R": "平均R(1取引)", "profit_factor": "プロフィットファクター", "final_equity": "最終資産",
    "total_return": "総リターン", "CAGR": "年率リターン(CAGR)", "monthly_mean": "月利 平均",
    "monthly_median": "月利 中央値", "monthly_std": "月利 標準偏差", "best_month": "最良月",
    "worst_month": "最悪月", "positive_months": "プラス月の割合", "max_drawdown": "最大ドローダウン",
    "sharpe_monthly": "シャープレシオ(月次→年率)",
}
PCT = {"win_rate", "total_return", "CAGR", "monthly_mean", "monthly_median", "monthly_std", "best_month",
       "worst_month", "positive_months", "max_drawdown"}


def format_stats(st: dict) -> str:
    lines = []
    for k, v in st.items():
        name = LABELS.get(k, k)
        if k in PCT:
            lines.append(f"{name:<24}: {v * 100:8.2f} %")
        elif isinstance(v, float):
            lines.append(f"{name:<24}: {v:10.3f}")
        else:
            lines.append(f"{name:<24}: {v}")
    return "\n".join(lines)


def yearly_table(res: BacktestResult) -> pd.DataFrame:
    m = res.monthly_returns()
    tab = (m * 100).groupby([m.index.year, m.index.month]).first().unstack()
    tab.columns = [f"{c}月" for c in tab.columns]
    tab["年間"] = ((1 + m).groupby(m.index.year).prod() - 1) * 100
    tab.index.name = "年"
    return tab.round(1)


def save_report(res: BacktestResult, out_dir: str | Path, title: str = "GreatestTrader backtest") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    st = res.stats()
    text = f"# {title}\n\n" + format_stats(st) + "\n\n## 月次リターン(%)\n\n" + yearly_table(res).to_string()
    if len(res.trades):
        by = res.trades.groupby(["strategy", "symbol"]).agg(trades=("R", "size"), avg_R=("R", "mean"),
                                                            sum_R=("R", "sum"), win=("R", lambda x: (x > 0).mean()))
        text += "\n\n## 戦略・通貨ペア別\n\n" + by.round(3).to_string()
    (out / "summary.txt").write_text(text, encoding="utf-8")
    res.trades.to_csv(out / "trades.csv", index=False)
    res.equity.to_csv(out / "equity.csv", header=True)
    yearly_table(res).to_csv(out / "monthly_returns.csv")
    try:
        _charts(res, out, title)
    except ImportError:
        pass
    return out


def _charts(res: BacktestResult, out: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    e = res.equity
    fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax[0].plot(e.index, e.values, lw=1.2, color="#1f5aa6")
    ax[0].set_yscale("log")
    ax[0].set_title(f"{title} - equity (log scale)")
    ax[0].grid(alpha=0.3)
    dd = e / e.cummax() - 1
    ax[1].fill_between(dd.index, dd.values * 100, 0, color="#c0392b", alpha=0.5)
    ax[1].set_ylabel("drawdown %")
    ax[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out / "equity_curve.png", dpi=110)
    plt.close(fig)
