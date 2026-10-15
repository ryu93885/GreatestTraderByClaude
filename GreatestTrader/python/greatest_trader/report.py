"""Backtest reporting: text summary, CSV files and (optional) charts."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .backtest import BacktestResult

LABELS = {
    "period": "期間", "trades": "取引回数", "months": "月数(全体)", "complete_months": "月数(完全月)",
    "trades_per_month": "月平均取引回数", "win_rate": "勝率", "avg_R": "平均R(1取引)",
    "profit_factor": "プロフィットファクター", "final_equity": "最終資産", "total_return": "総リターン",
    "CAGR": "年利(CAGR・複利)", "monthly_compound": "月利 複利換算(CAGRベース)",
    "monthly_mean": "月利 単純平均(完全月)", "monthly_geometric": "月利 幾何平均(完全月)",
    "monthly_median": "月利 中央値(完全月)", "monthly_std": "月利 標準偏差", "best_month": "最良月",
    "worst_month": "最悪月", "positive_months": "プラス月の割合", "max_drawdown": "最大DD(決済ベース)",
    "max_drawdown_mtm": "最大DD(含み損益込み)", "sharpe_monthly": "シャープレシオ(月次→年率)",
}
PCT = {"win_rate", "total_return", "CAGR", "monthly_compound", "monthly_mean", "monthly_geometric",
       "monthly_median", "monthly_std", "best_month", "worst_month", "positive_months", "max_drawdown",
       "max_drawdown_mtm"}


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
    """Monthly returns (%) per year plus the annual return (年間 = year-end / previous year-end - 1)."""
    m = res.monthly_returns()
    if len(m) == 0:
        return pd.DataFrame()
    tab = (m * 100).groupby([m.index.year, m.index.month]).first().unstack()
    tab = tab.reindex(columns=range(1, 13))
    tab.columns = [f"{c}月" for c in tab.columns]
    tab["年間"] = res.annual_returns()["return"] * 100
    tab.index.name = "年"
    return tab.round(2)


def annual_table(res: BacktestResult) -> pd.DataFrame:
    """Per-year annual return with start/end equity and a note for partial years."""
    a = res.annual_returns()
    if len(a) == 0:
        return pd.DataFrame()
    partial: dict[int, list[int]] = {}
    for p in res.partial_months():
        partial.setdefault(p.year, []).append(p.month)
    out = pd.DataFrame({
        "期首資産": a.start_equity.round(2),
        "期末資産": a.end_equity.round(2),
        "年利(%)": (a["return"] * 100).round(2),
        "月数": a.months,
    })
    notes = []
    for y in a.index:
        if y in partial:
            notes.append("部分期間（" + "・".join(f"{mm}月" for mm in partial[y]) + "は一部のみ）")
        elif a.loc[y, "months"] < 12:
            notes.append("部分期間")
        else:
            notes.append("")
    out["備考"] = notes
    out.index.name = "年"
    return out


def save_report(res: BacktestResult, out_dir: str | Path, title: str = "GreatestTrader backtest") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    st = res.stats()
    text = f"# {title}\n\n" + format_stats(st)
    if len(res.trades):
        partial = ", ".join(str(p) for p in res.partial_months()) or "なし"
        text += ("\n\n## 年利（各年）\n\n" + annual_table(res).to_string()
                 + "\n\n## 月利（%）\n\n" + yearly_table(res).to_string()
                 + f"\n\n部分月（統計の単純平均・中央値などから除外）: {partial}\n"
                 + "定義: 月利 = 月末資産 / 前月末資産 - 1、年利 = 年末資産 / 前年末資産 - 1（決済ベース、サーバー時間）\n")
        by = res.trades.groupby(["strategy", "symbol"]).agg(trades=("R", "size"), avg_R=("R", "mean"),
                                                            sum_R=("R", "sum"), win=("R", lambda x: (x > 0).mean()))
        text += "\n## 戦略・通貨ペア別\n\n" + by.round(3).to_string()
    (out / "summary.txt").write_text(text, encoding="utf-8")
    res.trades.to_csv(out / "trades.csv", index=False)
    res.equity.to_csv(out / "equity.csv", header=True)
    if len(res.trades):
        yearly_table(res).to_csv(out / "monthly_returns.csv")
        annual_table(res).to_csv(out / "annual_returns.csv")
        try:
            _charts(res, out, title)
        except ImportError:
            pass
    return out


def _charts(res: BacktestResult, out: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    e = pd.concat([pd.Series([res.initial_equity], index=[res.equity.index[0] - pd.Timedelta(seconds=1)]),
                   res.equity])
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
