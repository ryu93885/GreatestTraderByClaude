# GreatestTrader v1.1.0 — FX 自動売買システム

**月利 5% を目標**とする、複数戦略の FX ポートフォリオです。v1.1 では、目標を保ったまま**ドローダウンと月ごとの損失を抑える**リスク管理を追加し、目的別の**プリセット**で切り替えられるようにしました。

| | |
|---|---|
| 本稼働 | **Python**（MetaTrader 5 経由で発注） |
| テスト | **MQL5 EA**（MT5 ストラテジーテスター用。ライブ稼働にも利用可） |
| 戦略 | ① Night Reversion：日付変更直後の 14 通貨ペアで平均回帰を狙う ② Euro Session：欧州午前に EURUSD を売る |
| リスク管理（v1.1） | 通貨ペア別のリスク配分、合計リスク上限、同一通貨・同一方向の上限、DD 連動の縮小、日次・月次の停止（月次は全決済）、年末年始の新規停止 |

## プリセットと過去検証の結果（2007/01〜2022/03、コスト込み）

「月利（複利換算）」は、資産が実際に増えたペース（(1+CAGR)^(1/12)−1）です。

| プリセット | 月利（複利換算） | 最大 DD（含み損益込み） | 最悪月 | −2% を下回った月 | 向いている人 |
|---|---|---|---|---|---|
| **stable5（既定）** | **5.38%** | **−11.6%** | −4.75% | 27 / 182 | 月利 5% を目指したい |
| stable4 | 4.37% | −10.7% | −3.67% | 21 / 182 | DD を抑えつつ急変動にも備えたい（推奨の中間案） |
| safe2 | 2.00% | −11.7% | −2.00% | 0 / 182 | 毎月の損失を −2% 前後に抑えたい |
| classic_aggressive（v1.0 の設定） | 6.42% | −33.0% | −14.93% | 34 / 182 | 参考（保護機能なし） |

> ⚠ **月利 5%・最大 DD 20% 以内・最悪月 −2% の 3 つを同時に満たすプリセットはありません**（理由は [docs/05_安定化改修報告書.md](docs/05_安定化改修報告書.md)）。
> ⚠ 成績は取引コストに強く左右されます。コストが 0.5 pips 高いと stable5 の月利は 3.50% に下がります。本番の現実的な期待値は、stable5・stable4 で月利 3% 前後です。
> ⚠ 過去の検証結果は将来の利益を保証しません。必ずデモ口座で確認してから、リスクを半分にして始めてください。

---

## フォルダ構成

```
GreatestTrader/
├── README.md                  ← 本書
├── docs/
│   ├── 01_仕様説明書.md        ← 戦略・リスク管理・システムの仕様
│   ├── 02_取扱説明書.md        ← インストール・設定・運用・MT5 テストの手順
│   ├── 03_バックテスト報告書.md ← v1.0 の 3 プロファイルの検証結果
│   ├── 04_月利年利検証報告書.md ← 月利・年利（各年）の計算方法の検証
│   ├── 05_安定化改修報告書.md   ← v1.1 の DD 抑制改修・プリセットの検証結果と全表
│   └── images/
├── python/                    ← 本稼働システム（Python）
│   ├── setup_windows.bat      ← ① 初回セットアップ
│   ├── 1_check.bat            ← ② 接続確認
│   ├── 2_run_dryrun.bat       ← ③ ドライラン（注文なし）
│   ├── 3_run_live.bat         ← ④ 本番稼働（実際に発注）
│   ├── 4_download_data.bat    ← MT5 から過去データを取得
│   ├── 5_backtest.bat         ← Python でバックテスト
│   ├── config/config.yaml     ← 設定ファイル（preset: stable5）
│   ├── main.py
│   ├── greatest_trader/       ← プログラム本体（presets/ にプリセット）
│   └── tests/                 ← 自動テスト（pytest、62 件）
└── mql5/
    ├── Experts/GreatestTrader.mq5   ← MT5 用 EA（既定値 = stable5）
    └── Presets/*.set                ← プリセット（Stable5_Default / Stable4_Robust / Safe2_WorstMonth2pct / Classic_*）
```

## クイックスタート

### A. MT5 ストラテジーテスターで検証する（最初に推奨）

1. MT5 の [ファイル] → [データフォルダを開く] → `MQL5\Experts\` に `mql5/Experts/GreatestTrader.mq5` を、`MQL5\Presets\` に `mql5/Presets/*.set` をコピーします。
2. MetaEditor（F4）で `GreatestTrader.mq5` を開いてコンパイルします（F7）。
3. ストラテジーテスター（Ctrl+R）で次のとおり設定して [開始] を押します。
   - エキスパート：GreatestTrader
   - 銘柄：EURUSD
   - 時間足：M15
   - モデル：1 分足 OHLC（最終確認はリアルティック）
   - パラメータ：使うプリセットの .set を読み込む（例：`GreatestTrader_Stable5_Default.set`）

### B. Python で本稼働する（Windows）

1. MT5 を起動・ログインし、[アルゴリズム取引] を ON にします。
2. `python\setup_windows.bat` を実行します。
3. `python\config\config.yaml` を編集します（シンボルの接尾辞、手数料、`preset:`）。
4. `1_check.bat` で接続を確認 → `2_run_dryrun.bat` でドライラン → デモ口座で `3_run_live.bat` を実行します。

詳細は **[docs/02_取扱説明書.md](docs/02_取扱説明書.md)** を参照してください。

## 必要なレバレッジ（合計建玉の最大 ÷ 口座資産）

| stable5 | stable4 | safe2 | classic_conservative / standard / aggressive |
|---|---|---|---|
| 約 67 倍 | 約 46 倍 | 約 18 倍 | 約 30 / 58 / 87 倍 |

国内口座（最大 25 倍）では safe2 を使ってください。stable5 には 100 倍以上の口座が必要です。

## 動作環境

- Windows 10 / 11 / Server、MetaTrader 5、Python 3.10 以上（64 ビット）
- 推奨：ECN / RAW 口座（低スプレッド＋手数料型）、24 時間稼働の VPS
