<#
.SYNOPSIS
    AWS リソースの読み取り専用インベントリ（全リージョン横断）。

.DESCRIPTION
    AWS を片付ける前の「消し忘れチェック」用スクリプトです。
    EC2 インスタンス / EBS ボリューム / スナップショット / AMI / Elastic IP /
    キーペア / セキュリティグループ を全リージョンで一覧化し、1つの txt に出力します。

    呼び出すのは describe / list / get-caller-identity のみで、
    削除・変更・起動停止などの操作は一切行いません。

.PREREQUISITES
    - AWS CLI v2 がインストール済み  (https://aws.amazon.com/cli/)
    - `aws configure` で認証情報が設定済み
      （可能であれば ReadOnlyAccess 権限のアクセスキーを使うのが安全）

.NOTES
    * このスクリプトはリソースを削除しません。出力内容を確認のうえ、
      削除はマネジメントコンソール等で手動実施してください。
    * 削除の推奨順序はレポート末尾に記載します。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Inventory-AwsResources.ps1
#>
[CmdletBinding()]
param(
    [string]$OutFile = (Join-Path ([Environment]::GetFolderPath('Desktop')) ("aws_inventory_{0:yyyyMMdd_HHmmss}.txt" -f (Get-Date)))
)

function Invoke-AwsJson {
    param([string[]]$CliArgs)
    $raw = & aws @CliArgs --output json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
    try { $raw | ConvertFrom-Json } catch { $null }
}

# ---- 前提チェック ----
& aws --version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'AWS CLI が見つかりません。https://aws.amazon.com/cli/ から導入し「aws configure」を実行してください。'
    return
}
$ident = Invoke-AwsJson @('sts', 'get-caller-identity')
if (-not $ident) {
    Write-Host '認証に失敗しました。「aws configure」で認証情報を設定してください。'
    return
}

$sb    = New-Object System.Text.StringBuilder
$flags = New-Object System.Collections.Generic.List[string]
function W([string]$t = '') { [void]$sb.AppendLine($t) }

W '=========================================================='
W ' AWS インベントリ（読み取り専用）'
W (' アカウント : {0}' -f $ident.Account)
W (' 実行日時   : {0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
W ' ※ describe/list のみ。削除・変更はしていません。'
W '=========================================================='

# ---- リージョン一覧 ----
$regionsObj = Invoke-AwsJson @('ec2', 'describe-regions', '--all-regions')
$regions = if ($regionsObj) { $regionsObj.Regions.RegionName | Sort-Object } else { @('ap-southeast-2', 'ap-northeast-1', 'us-east-1') }

foreach ($r in $regions) {
    $buf = New-Object System.Collections.Generic.List[string]

    # EC2 インスタンス
    $inst = Invoke-AwsJson @('ec2', 'describe-instances', '--region', $r)
    if ($inst) {
        foreach ($res in $inst.Reservations) {
            foreach ($i in $res.Instances) {
                $buf.Add(('  [EC2] {0}  {1}  {2}  起動:{3}' -f $i.InstanceId, $i.State.Name, $i.InstanceType, $i.LaunchTime))
                if ($i.State.Name -in 'stopped', 'stopping') { $flags.Add(("{0}: 停止中インスタンス {1} が残存（不要なら terminate）" -f $r, $i.InstanceId)) }
            }
        }
    }

    # EBS ボリューム
    $volO = Invoke-AwsJson @('ec2', 'describe-volumes', '--region', $r)
    if ($volO) {
        foreach ($v in $volO.Volumes) {
            $att = if ($v.Attachments -and $v.Attachments.Count -gt 0) { $v.Attachments[0].InstanceId } else { '(未アタッチ)' }
            $buf.Add(('  [EBS] {0}  {1}GiB  {2}  attach:{3}' -f $v.VolumeId, $v.Size, $v.State, $att))
            if ($v.State -eq 'available') { $flags.Add(("{0}: 未アタッチのEBS {1} が残存（課金対象・不要なら削除）" -f $r, $v.VolumeId)) }
        }
    }

    # スナップショット（自分所有）
    $snapO = Invoke-AwsJson @('ec2', 'describe-snapshots', '--owner-ids', 'self', '--region', $r)
    if ($snapO) { foreach ($s in $snapO.Snapshots) { $buf.Add(('  [SNAP] {0}  {1}GiB  {2}' -f $s.SnapshotId, $s.VolumeSize, $s.StartTime)) } }

    # AMI（自分所有）
    $amiO = Invoke-AwsJson @('ec2', 'describe-images', '--owners', 'self', '--region', $r)
    if ($amiO) { foreach ($a in $amiO.Images) { $buf.Add(('  [AMI] {0}  {1}' -f $a.ImageId, $a.Name)) } }

    # Elastic IP
    $eipO = Invoke-AwsJson @('ec2', 'describe-addresses', '--region', $r)
    if ($eipO) {
        foreach ($e in $eipO.Addresses) {
            $assoc = if ($e.AssociationId) { '割当済' } else { '未割当' }
            $buf.Add(('  [EIP] {0}  {1}' -f $e.PublicIp, $assoc))
            if (-not $e.AssociationId) { $flags.Add(("{0}: 未割当のElastic IP {1}（課金対象・リリース検討）" -f $r, $e.PublicIp)) }
        }
    }

    # キーペア
    $keyO = Invoke-AwsJson @('ec2', 'describe-key-pairs', '--region', $r)
    if ($keyO -and $keyO.KeyPairs) { $buf.Add(('  [KEY] {0}' -f (($keyO.KeyPairs.KeyName) -join ', '))) }

    # セキュリティグループ（RDP/SSH が 0.0.0.0/0 開放か判定）
    $sgO = Invoke-AwsJson @('ec2', 'describe-security-groups', '--region', $r)
    if ($sgO) {
        foreach ($sg in $sgO.SecurityGroups) {
            foreach ($perm in $sg.IpPermissions) {
                $from = $perm.FromPort
                $to   = $perm.ToPort
                $coversAdmin = ($null -eq $from) -or
                               ($from -le 22 -and $to -ge 22) -or
                               ($from -le 3389 -and $to -ge 3389)
                $open = $perm.IpRanges | Where-Object { $_.CidrIp -eq '0.0.0.0/0' }
                if ($open -and $coversAdmin) {
                    $flags.Add(("{0}: SG {1}({2}) が 0.0.0.0/0 で port {3}-{4} を開放（起動前に制限を）" -f $r, $sg.GroupId, $sg.GroupName, $from, $to))
                }
            }
        }
    }

    if ($buf.Count -gt 0) {
        W ''
        W ("---- リージョン: {0} ----" -f $r)
        $buf | ForEach-Object { W $_ }
    }
}

W ''
W '========== 注意ポイント（自動検出） =========='
if ($flags.Count -gt 0) { $flags | ForEach-Object { W (' - {0}' -f $_) } } else { W ' 特記事項は検出されませんでした。' }

W ''
W '========== 片付けの推奨順序（手動で実施） =========='
W ' 1) 必要データを退避（MT5 データフォルダ/EA/設定 など）'
W ' 2) 本一覧で全リージョンの残存リソースを確認'
W ' 3) EC2 インスタンスを Terminate'
W ' 4) 残った EBS ボリュームを削除'
W ' 5) スナップショット削除 / AMI の登録解除'
W ' 6) Elastic IP をリリース'
W ' 7) 不要なキーペア / セキュリティグループを削除'
W ' 8) 当面アカウントを残すなら: root に MFA、root のアクセスキー削除、CloudTrail/請求アラート有効化'
W ' ※削除・リリース・スナップショット削除は取り消せません。内容を確認してから実施してください。'

Set-Content -LiteralPath $OutFile -Value ($sb.ToString()) -Encoding UTF8
Write-Host ("インベントリ出力: {0}  (注意ポイント: {1} 件)" -f $OutFile, $flags.Count)
