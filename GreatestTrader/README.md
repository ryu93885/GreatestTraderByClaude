# GreatestTrader v1.0.1 — FX 自動売買システム

**月利 5% を目標**とする、複数戦略の FX ポートフォリオです。

| | |
|---|---|
| 本稼働 | **Python**（MetaTrader 5 経由で発注） |
| テスト | **MQL5 EA**（MT5 ストラテジーテスター用。ライブ稼働にも利用可） |
| 戦略 | ① Night Reversion：日付変更直後の 14 通貨ペアで平均回帰を狙う ② Euro Session：欧州午前に EURUSD を売る |
| 過去検証（2007/01〜2022/03、コスト込み、複利換算の月利） | aggressive：月利 6.3%（2011 年以降は 4.8%）、最大 DD −34%／standard：月利 4.6%、最大 DD −27% |

> ⚠ 過去の検証結果は将来の利益を保証しません。成績は取引コストに強く左右されます。必ずデモ口座で確認してから、少額で始めてください。

---

## フォルダ構成

```
GreatestTrader/
├── README.md                  ← 本書
├── docs/
│   ├── 01_仕様説明書.md        ← 戦略・リスク管理・システムの仕様
│   ├── 02_取扱説明書.md        ← インストール・設定・運用・MT5 テストの手順
│   ├── 03_バックテスト報告書.md ← 検証結果の詳細
│   ├── 04_月利年利検証報告書.md ← 月利・年利（各年）の計算検証と全表
│   └── images/
├── python/                    ← 本稼働システム（Python）
│   ├── setup_windows.bat      ← ① 初回セットアップ
│   ├── 1_check.bat            ← ② 接続確認
│   ├── 2_run_dryrun.bat       ← ③ ドライラン（注文なし）
│   ├── 3_run_live.bat         ← ④ 本番稼働（実際に発注）
│   ├── 4_download_data.bat    ← MT5 から過去データを取得
│   ├── 5_backtest.bat         ← Python でバックテスト
│   ├── config/config.yaml     ← 設定ファイル
│   ├── main.py
│   ├── greatest_trader/       ← プログラム本体
│   └── tests/                 ← 自動テスト（pytest）
└── mql5/
    ├── Experts/GreatestTrader.mq5   ← MT5 用 EA
    └── Presets/*.set                ← 設定プリセット（Conservative / Standard / Aggressive_5pct / HighCostBroker）
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
   - パラメータ：プリセットを読み込む

### B. Python で本稼働する（Windows）

1. MT5 を起動・ログインし、[アルゴリズム取引] を ON にします。
2. `python\setup_windows.bat` を実行します。
3. `python\config\config.yaml` を編集します（シンボルの接尾辞、手数料、リスクプロファイル）。
4. `1_check.bat` で接続を確認 → `2_run_dryrun.bat` でドライラン → デモ口座で `3_run_live.bat` を実行します。

詳細は **[docs/02_取扱説明書.md](docs/02_取扱説明書.md)** を参照してください。

月利・年利（各年）の全表と計算方法は **[docs/04_月利年利検証報告書.md](docs/04_月利年利検証報告書.md)** にあります。

## リスクプロファイル

「月利・複利換算」は、資産が実際に増えたペース（(1+CAGR)^(1/12)−1）です。

| profile | 1 取引のリスク | 過去検証の月利・複利換算（2007〜2022 年／2011〜2022 年） | 最大 DD（含み損益込み） |
|---|---|---|---|
| conservative | 0.5% | 2.6% ／ 2.1% | −18% |
| standard | 1.0% | 4.6% ／ 3.6% | −27% |
| **aggressive** | **1.5%** | **6.3% ／ 4.8%** | **−34%** |

## 動作環境

- Windows 10 / 11 / Server、MetaTrader 5、Python 3.10 以上（64 ビット）
- 推奨：ECN / RAW 口座（低スプレッド＋手数料型）、24 時間稼働の VPS
