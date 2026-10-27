<#
.SYNOPSIS
    Windows PC セキュリティ自己診断（読み取り専用）。

.DESCRIPTION
    現在のPCのセキュリティ設定を点検し、所見レポートを1つの txt に出力します。
    describe/get 系の読み取りのみを行い、設定の変更・削除は一切しません。

    点検項目:
      - OS / 最終更新 (HotFix)
      - Windows Defender（有効 / リアルタイム保護 / 定義の鮮度）
      - ファイアウォール（各プロファイル）
      - リモートデスクトップ(RDP) 有効状態 / NLA / 3389 の待受
      - 保存済み資格情報(cmdkey) と RDP 接続履歴
      - ローカル管理者 / Guest / パスワード不要アカウント
      - 自動ログオン
      - UAC
      - SMBv1
      - BitLocker（システムドライブ）
      - 全インターフェイスで待受している TCP ポート
      - アカウントロックアウトのしきい値

.NOTES
    * 完全な結果を得るには「管理者として実行」してください（一部項目は権限が必要）。
    * 出力レポートにはユーザー名・ホスト名・接続履歴が含まれます。共有時はご注意ください。
    * 本スクリプトは設定を一切変更しません（検知・点検のみ）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Invoke-PcSecurityAudit.ps1
#>
[CmdletBinding()]
param(
    [string]$OutFile = (Join-Path ([Environment]::GetFolderPath('Desktop')) ("pc_security_audit_{0:yyyyMMdd_HHmmss}.txt" -f (Get-Date)))
)

$findings = New-Object System.Collections.Generic.List[object]

function Add-Finding {
    param(
        [string]$Category,
        [ValidateSet('OK', 'WARN', 'CHECK', 'INFO')][string]$Level,
        [string]$Item,
        [string]$Detail,
        [string]$Recommend = ''
    )
    $findings.Add([pscustomobject]@{
        Category  = $Category
        Level     = $Level
        Item      = $Item
        Detail    = $Detail
        Recommend = $Recommend
    })
}

$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltinRole]::Administrator)

# ---------------- OS / Updates ----------------
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    Add-Finding 'システム' 'INFO' 'OS' ("{0} (Build {1})" -f $os.Caption, $os.BuildNumber)
} catch { Add-Finding 'システム' 'CHECK' 'OS' "取得失敗: $($_.Exception.Message)" }

try {
    $lastHot = Get-HotFix -ErrorAction SilentlyContinue | Where-Object InstalledOn | Sort-Object InstalledOn -Descending | Select-Object -First 1
    if ($lastHot) {
        $days = (New-TimeSpan -Start $lastHot.InstalledOn -End (Get-Date)).Days
        $lvl  = if ($days -gt 60) { 'WARN' } else { 'OK' }
        $rec  = if ($lvl -eq 'WARN') { 'Windows Update を確認して最新の更新を適用してください' } else { '' }
        Add-Finding '更新' $lvl '最終更新(HotFix)' ("{0} ({1}日前)" -f $lastHot.InstalledOn.ToString('yyyy-MM-dd'), $days) $rec
    }
} catch {}

# ---------------- Windows Defender ----------------
try {
    $mp = Get-MpComputerStatus -ErrorAction Stop
    Add-Finding 'ウイルス対策' ($(if ($mp.AntivirusEnabled) { 'OK' } else { 'WARN' })) 'Defender有効' $mp.AntivirusEnabled '無効の場合は他社AVの状態を確認'
    Add-Finding 'ウイルス対策' ($(if ($mp.RealTimeProtectionEnabled) { 'OK' } else { 'WARN' })) 'リアルタイム保護' $mp.RealTimeProtectionEnabled '無効なら有効化を推奨'
    $sigAge = [int]$mp.AntivirusSignatureAge
    Add-Finding 'ウイルス対策' ($(if ($sigAge -le 3) { 'OK' } else { 'WARN' })) '定義ファイル経過日数' ("{0} 日" -f $sigAge) ($(if ($sigAge -gt 3) { '定義を更新してください' } else { '' }))
} catch { Add-Finding 'ウイルス対策' 'CHECK' 'Defender' '取得失敗（他社AV使用か権限不足の可能性）' }

# ---------------- Firewall ----------------
try {
    Get-NetFirewallProfile -ErrorAction Stop | ForEach-Object {
        Add-Finding 'ファイアウォール' ($(if ($_.Enabled) { 'OK' } else { 'WARN' })) ("プロファイル:{0}" -f $_.Name) ("Enabled={0}" -f $_.Enabled) ($(if (-not $_.Enabled) { '有効化を推奨' } else { '' }))
    }
} catch { Add-Finding 'ファイアウォール' 'CHECK' '状態' "取得失敗: $($_.Exception.Message)" }

# ---------------- RDP ----------------
try {
    $deny  = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -ErrorAction Stop).fDenyTSConnections
    $rdpOn = ($deny -eq 0)
    Add-Finding 'RDP' ($(if ($rdpOn) { 'WARN' } else { 'OK' })) 'リモートデスクトップ' ($(if ($rdpOn) { '有効' } else { '無効' })) ($(if ($rdpOn) { '使わないなら無効化。使うなら受信を自分のIPに限定し NLA 有効に' } else { '' }))

    if ($rdpOn) {
        $nla = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name UserAuthentication -ErrorAction SilentlyContinue).UserAuthentication
        Add-Finding 'RDP' ($(if ($nla -eq 1) { 'OK' } else { 'WARN' })) 'NLA(ネットワークレベル認証)' ($(if ($nla -eq 1) { '有効' } else { '無効' })) ($(if ($nla -ne 1) { '有効化を推奨' } else { '' }))

        $listen = Get-NetTCPConnection -LocalPort 3389 -State Listen -ErrorAction SilentlyContinue
        $allIf  = $listen | Where-Object { $_.LocalAddress -in '0.0.0.0', '::' }
        if ($allIf) {
            Add-Finding 'RDP' 'WARN' '3389ポート待受' '全インターフェイス(0.0.0.0)で待受' 'ファイアウォールで受信元を自分のIP/LANに限定'
        }
    }
} catch { Add-Finding 'RDP' 'CHECK' '状態' "取得失敗: $($_.Exception.Message)" }

# ---------------- 保存済み資格情報 / RDP接続履歴 ----------------
try {
    $cmd = (cmdkey /list 2>$null | Select-String 'Target:' | ForEach-Object { ($_ -replace '.*Target:\s*', '').Trim() })
    if ($cmd) { Add-Finding '資格情報' 'CHECK' '保存済み資格情報(cmdkey)' ($cmd -join '; ') '不要な保存資格情報は削除を検討（cmdkey /delete）' }
} catch {}
try {
    $srv = Get-ChildItem 'HKCU:\Software\Microsoft\Terminal Server Client\Servers' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty PSChildName
    if ($srv) { Add-Finding '資格情報' 'INFO' 'RDP接続履歴' ($srv -join '; ') '身に覚えのない接続先がないか確認' }
} catch {}

# ---------------- ローカル管理者 / Guest / パスワード ----------------
try {
    $admins = Get-LocalGroupMember -Group 'Administrators' -ErrorAction Stop | Select-Object -ExpandProperty Name
    Add-Finding 'アカウント' ($(if ($admins.Count -le 2) { 'OK' } else { 'CHECK' })) '管理者グループのメンバー' ($admins -join '; ') '不要な管理者アカウントは削除/降格を検討'
} catch { Add-Finding 'アカウント' 'CHECK' '管理者グループ' '取得失敗（管理者権限が必要な場合あり）' }

try {
    $guest = Get-LocalUser -Name 'Guest' -ErrorAction SilentlyContinue
    if ($guest) {
        Add-Finding 'アカウント' ($(if ($guest.Enabled) { 'WARN' } else { 'OK' })) 'Guestアカウント' ($(if ($guest.Enabled) { '有効' } else { '無効' })) ($(if ($guest.Enabled) { '無効化を推奨' } else { '' }))
    }
    $noPw = Get-LocalUser -ErrorAction SilentlyContinue | Where-Object { $_.Enabled -and -not $_.PasswordRequired }
    if ($noPw) { Add-Finding 'アカウント' 'WARN' 'パスワード不要の有効アカウント' (($noPw | Select-Object -ExpandProperty Name) -join '; ') 'パスワードを設定してください' }
} catch {}

# ---------------- 自動ログオン ----------------
try {
    $win = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' -ErrorAction Stop
    if ($win.AutoAdminLogon -eq '1') {
        $hasPw = [bool]$win.DefaultPassword
        Add-Finding 'ログオン' 'WARN' '自動ログオン' ('有効' + $(if ($hasPw) { ' / パスワードが平文でレジストリに保存' } else { '' })) '自動ログオンは無効化を推奨'
    } else {
        Add-Finding 'ログオン' 'OK' '自動ログオン' '無効'
    }
} catch {}

# ---------------- UAC ----------------
try {
    $lua = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name EnableLUA -ErrorAction Stop).EnableLUA
    Add-Finding 'UAC' ($(if ($lua -eq 1) { 'OK' } else { 'WARN' })) 'UAC' ($(if ($lua -eq 1) { '有効' } else { '無効' })) ($(if ($lua -ne 1) { '有効化を推奨' } else { '' }))
} catch {}

# ---------------- SMBv1 ----------------
try {
    $smb = Get-SmbServerConfiguration -ErrorAction Stop
    Add-Finding 'SMB' ($(if ($smb.EnableSMB1Protocol) { 'WARN' } else { 'OK' })) 'SMBv1' ($(if ($smb.EnableSMB1Protocol) { '有効' } else { '無効' })) ($(if ($smb.EnableSMB1Protocol) { '脆弱な旧プロトコル。無効化を推奨' } else { '' }))
} catch { Add-Finding 'SMB' 'CHECK' 'SMBv1' '取得に管理者権限が必要な場合があります' }

# ---------------- BitLocker ----------------
try {
    $bl = Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction Stop
    $on = ($bl.ProtectionStatus -eq 'On')
    Add-Finding '暗号化' ($(if ($on) { 'OK' } else { 'CHECK' })) ("BitLocker({0})" -f $env:SystemDrive) $bl.ProtectionStatus ($(if (-not $on) { 'ノートPC/持ち出し機は暗号化を検討' } else { '' }))
} catch { Add-Finding '暗号化' 'CHECK' 'BitLocker' '取得失敗（Home版/権限不足の可能性）' }

# ---------------- 外部公開の待受ポート ----------------
try {
    $listenPorts = Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalAddress -in '0.0.0.0', '::' } |
        Select-Object -ExpandProperty LocalPort -Unique | Sort-Object
    if ($listenPorts) {
        Add-Finding 'ネットワーク' 'INFO' '全IF待受TCPポート' ($listenPorts -join ', ') '不要な受信はファイアウォールで制限'
    }
} catch {}

# ---------------- アカウントロックアウト ----------------
try {
    $na   = net accounts 2>$null
    $lock = $na | Select-String 'ロックアウト|Lockout'
    if ($lock) { Add-Finding 'ポリシー' 'INFO' 'アカウントロックアウト' (($lock | ForEach-Object { $_.ToString().Trim() }) -join ' / ') }
} catch {}

# ================= 出力 =================
$order = @{ 'WARN' = 0; 'CHECK' = 1; 'INFO' = 2; 'OK' = 3 }
$sorted = $findings | Sort-Object @{ Expression = { $order[$_.Level] } }, Category

$out = New-Object System.Collections.Generic.List[string]
$out.Add('==========================================================')
$out.Add(' Windows PC セキュリティ自己診断レポート')
$out.Add((' 実行日時     : {0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
$out.Add((' コンピュータ : {0}' -f $env:COMPUTERNAME))
$out.Add((' 管理者で実行 : {0}' -f $IsAdmin))
$out.Add('==========================================================')
$out.Add(' 凡例: OK=問題なし / WARN=要対応 / CHECK=要確認(権限等) / INFO=参考')
$out.Add(' ※本レポートは読み取り専用の点検結果です。設定は変更していません。')
$out.Add(' ※ユーザー名・ホスト名・接続履歴を含むため、共有時はご注意ください。')
$out.Add('----------------------------------------------------------')

foreach ($f in $sorted) {
    $out.Add(('[{0,-5}] {1} / {2}: {3}' -f $f.Level, $f.Category, $f.Item, $f.Detail))
    if ($f.Recommend) { $out.Add(('          -> 推奨: {0}' -f $f.Recommend)) }
}

$warnCount  = ($findings | Where-Object Level -eq 'WARN').Count
$checkCount = ($findings | Where-Object Level -eq 'CHECK').Count
$out.Add('----------------------------------------------------------')
$out.Add((' サマリ : 要対応(WARN) {0} 件 / 要確認(CHECK) {1} 件' -f $warnCount, $checkCount))

Set-Content -LiteralPath $OutFile -Value ($out -join "`r`n") -Encoding UTF8

Write-Host ("診断完了: {0}  (WARN:{1} / CHECK:{2})" -f $OutFile, $warnCount, $checkCount)
if (-not $IsAdmin) { Write-Host '※ 管理者として実行すると、より多くの項目を取得できます。' }
