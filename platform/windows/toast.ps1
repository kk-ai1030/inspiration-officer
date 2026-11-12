# Windows toast for the inspiration run. Clicking it (or the button) opens the review page in the browser.
# Called by platform/review_server.py: powershell.exe -File toast.ps1 -Payload <json {title, body, url}>
# Works with Windows PowerShell 5.1 on Windows 10/11; no extra modules needed.
param([Parameter(Mandatory=$true)][string]$Payload)
$ErrorActionPreference = "Stop"
$p = Get-Content -Raw -Encoding UTF8 $Payload | ConvertFrom-Json
function X([string]$s) { [Security.SecurityElement]::Escape($s) }
$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$btn = '开始评价'
if ($p.button) { $btn = [string]$p.button }
if ($p.url) {
  $head = "<toast activationType=`"protocol`" launch=`"$(X $p.url)`" duration=`"long`">"
  $acts = "<actions><action content=`"$(X $btn)`" activationType=`"protocol`" arguments=`"$(X $p.url)`"/></actions>"
} else {
  $head = "<toast duration=`"long`">"; $acts = ""
}
$xml = "$head<visual><binding template=`"ToastGeneric`"><text>$(X $p.title)</text><text>$(X $p.body)</text></binding></visual>$acts</toast>"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$t = [Windows.UI.Notifications.ToastNotification]::new($doc)
$t.ExpirationTime = [DateTimeOffset]::Now.AddHours(12)
$t.Tag = "insp"; $t.Group = "insp"
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId)
$notifier.Show($t)
# --- diagnostics: Show() returns silently even when Windows drops the toast, so report what we can see ---
Start-Sleep -Milliseconds 800
function RegVal($path, $name) { try { (Get-ItemProperty -Path $path -Name $name -ErrorAction Stop).$name } catch { $null } }
$hist = $null
try { $hist = @([Windows.UI.Notifications.ToastNotificationManager]::History.GetHistory($AppId)).Count } catch { }
$diag = [ordered]@{
  setting      = "$($notifier.Setting)"   # Enabled / DisabledForApplication / DisabledForUser / DisabledByGroupPolicy / DisabledByManifest
  history      = $hist                     # toasts of this app currently in the notification centre
  app_enabled  = RegVal "HKCU:\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\$AppId" "Enabled"
  global_toast = RegVal "HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications" "ToastEnabled"
  session_id   = (Get-Process -Id $PID).SessionId
  user         = "$env:USERNAME"
}
Write-Output ("TOASTDIAG " + ($diag | ConvertTo-Json -Compress))
