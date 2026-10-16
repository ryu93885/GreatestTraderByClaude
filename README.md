# GreatestTraderByClaude

FX 自動売買システム **GreatestTrader v1.1.0**（目標：月利 5%。v1.1 でドローダウン抑制のプリセットを追加）

| ファイル / フォルダ | 内容 |
|---|---|
| [`GreatestTrader_v1.1.0.zip`](GreatestTrader_v1.1.0.zip) | **配布用 zip**（展開してすぐに使える完成版） |
| [`GreatestTrader/`](GreatestTrader/) | zip と同じ内容（閲覧用） |
| [`GreatestTrader/docs/01_仕様説明書.md`](GreatestTrader/docs/01_仕様説明書.md) | 仕様説明書 |
| [`GreatestTrader/docs/02_取扱説明書.md`](GreatestTrader/docs/02_取扱説明書.md) | 取扱説明書 |
| [`GreatestTrader/docs/03_バックテスト報告書.md`](GreatestTrader/docs/03_バックテスト報告書.md) | バックテスト報告書（v1.0 の 3 プロファイル） |
| [`GreatestTrader/docs/04_月利年利検証報告書.md`](GreatestTrader/docs/04_月利年利検証報告書.md) | 月利・年利（各年）検証報告書 |
| [`GreatestTrader/docs/05_安定化改修報告書.md`](GreatestTrader/docs/05_安定化改修報告書.md) | 安定化改修報告書（v1.1：ドローダウン抑制・プリセット） |

| プリセット | 月利（複利換算） | 最大 DD | 最悪月 |
|---|---|---|---|
| **stable5（既定）** | 5.38% | −11.6% | −4.75% |
| stable4 | 4.37% | −10.7% | −3.67% |
| safe2 | 2.00% | −11.7% | −2.00% |

（過去検証 2007/01〜2022/03、コスト込み。3 つの目標〈月利 5%・DD 20% 以内・最悪月 −2%〉を同時に満たすプリセットはありません）

- 本稼働：Python（`GreatestTrader/python`）＋ MetaTrader 5
- テスト：MQL5 EA（`GreatestTrader/mql5/Experts/GreatestTrader.mq5`）

⚠ 過去の検証結果は将来の利益を保証しません。必ずデモ口座で確認してから利用してください。
