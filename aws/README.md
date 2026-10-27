# aws — AWS 片付け支援（読み取り専用）

AWS の利用をやめて片付ける際の **「消し忘れチェック」** に使うツールを置きます。
**削除は行いません。** 一覧を見て、削除はご自身でコンソール等から実施してください。

## Inventory-AwsResources.ps1 — リソース棚卸し（全リージョン横断）

EC2 / EBS / スナップショット / AMI / Elastic IP / キーペア / セキュリティグループ を
全リージョンで一覧化し、1つの txt に出力します。呼び出すのは `describe` / `list` 系のみです。

### 前提

- AWS CLI v2 をインストール: https://aws.amazon.com/cli/
- 認証情報を設定: `aws configure`
  - 可能なら **ReadOnlyAccess 権限**のアクセスキーを使うと安全です。

### 実行方法

```powershell
powershell -ExecutionPolicy Bypass -File .\Inventory-AwsResources.ps1
```

実行後、デスクトップに `aws_inventory_YYYYMMDD_HHMMSS.txt` が出力されます。
末尾に「注意ポイント（未アタッチEBS・未割当EIP・開放SG・停止中インスタンスなど）」と
「片付けの推奨順序」をまとめます。

### 片付けの推奨順序（手動）

1. 必要データを退避（MT5 データフォルダ / EA / 設定 など）
2. 本一覧で全リージョンの残存リソースを確認
3. EC2 インスタンスを **Terminate**
4. 残った **EBS ボリューム**を削除
5. **スナップショット削除** / **AMI の登録解除**
6. **Elastic IP** をリリース
7. 不要な **キーペア / セキュリティグループ** を削除
8. 当面アカウントを残すなら: root に **MFA**、root の**アクセスキー削除**、**CloudTrail / 請求アラート**有効化

> ⚠️ 削除・リリース・スナップショット削除は**取り消せません**。必ず内容を確認してから実施してください。
