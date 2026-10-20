# GreatestTraderByClaude

FX 自動売買システム **GreatestTrader v1.2.0（最終モデル）**（目標：月利 5%。v1.1 でドローダウン抑制のプリセット、v1.2 で長期運用の仕組みを追加）

| ファイル / フォルダ | 内容 |
|---|---|
| [`GreatestTrader_v1.2.0.zip`](GreatestTrader_v1.2.0.zip) | **配布用 zip**（展開してすぐに使える完成版） |
| [`GreatestTrader/`](GreatestTrader/) | zip と同じ内容（閲覧用） |
| [`GreatestTrader/docs/01_仕様説明書.md`](GreatestTrader/docs/01_仕様説明書.md) | 仕様説明書 |
| [`GreatestTrader/docs/02_取扱説明書.md`](GreatestTrader/docs/02_取扱説明書.md) | 取扱説明書 |
| [`GreatestTrader/docs/03_バックテスト報告書.md`](GreatestTrader/docs/03_バックテスト報告書.md) | バックテスト報告書（v1.0 の 3 プロファイル） |
| [`GreatestTrader/docs/04_月利年利検証報告書.md`](GreatestTrader/docs/04_月利年利検証報告書.md) | 月利・年利（各年）検証報告書 |
| [`GreatestTrader/docs/05_安定化改修報告書.md`](GreatestTrader/docs/05_安定化改修報告書.md) | 安定化改修報告書（v1.1：ドローダウン抑制・プリセット） |
| [`GreatestTrader/docs/06_長期運用評価報告書.md`](GreatestTrader/docs/06_長期運用評価報告書.md) | 長期運用評価報告書（v1.2：長期の再評価・口座規模の上限・運用手順） |
| [`GreatestTrader/docs/07_実口座環境テスター検証報告書.md`](GreatestTrader/docs/07_実口座環境テスター検証報告書.md) | 実口座環境（XM・Exness）の MT5 テスター検証と推奨モデル（Exness Zero・中核 4 ペア） |

| プリセット | 月利（口座が小さいうち） | 1 万ドルから 15 年（20 ロット上限込み） | 最大 DD | 最悪月 |
|---|---|---|---|---|
| **stable5（既定・推奨）** | 5.38% | 3.07% | −11.6% | −4.75% |
| stable4 | 4.37% | 2.93% | −10.7% | −3.67% |
| safe2 | 2.00% | 2.00% | −11.7% | −2.00% |

（過去検証 2007/01〜2022/03、コスト込み。3 つの目標〈月利 5%・DD 20% 以内・最悪月 −2%〉を同時に満たすプリセットはありません。口座が約 6.6 万ドルを超えると、深夜の流動性を考えた 1 注文 20 ロットの上限により利益は金額でほぼ一定になります）

- 本稼働：Python（`GreatestTrader/python`）＋ MetaTrader 5
- テスト：MQL5 EA（`GreatestTrader/mql5/Experts/GreatestTrader.mq5`）

⚠ 過去の検証結果は将来の利益を保証しません。必ずデモ口座で確認してから利用してください。
