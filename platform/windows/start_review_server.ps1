# 启动本地评价页服务（内置收货员：自动收回网页里 @灵感官 的运行结果）
# 用法（包根目录的 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File platform\windows\start_review_server.ps1              # 现在启动并打开评价页
#   powershell -ExecutionPolicy Bypass -File platform\windows\start_review_server.ps1 -Autostart   # 另外登记“登录时自动启动”
# 说明：-Autostart 只是登录时启动一个常驻进程，不会定时发起任何运行；收货员只读轮询，不花 token。
# 输入：config.json；~\.sciencepal\run_token（API Key，第一次会询问）
param([string]$PkgDir = (Resolve-Path "$PSScriptRoot\..\..").Path, [string]$Config = "config.json", [switch]$Autostart)
$ErrorActionPreference = "Stop"
$py = (Get-Command python).Source
$tokDir = Join-Path $env:USERPROFILE ".sciencepal"
$tokFile = Join-Path $tokDir "run_token"
if (-not (Test-Path $tokFile)) {
  New-Item -ItemType Directory -Force -Path $tokDir | Out-Null
  $sec = Read-Host "粘贴 API Key（sp_live_...，输入不回显）" -AsSecureString
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  [IO.File]::WriteAllText($tokFile, $plain.Trim())
  Write-Host "已保存到 $tokFile"
}
$cfgPath = Join-Path $PkgDir $Config
if ($Autostart) {
  $pyw = Join-Path (Split-Path $py) "pythonw.exe"
  if (-not (Test-Path $pyw)) { $pyw = $py }
  $a = New-ScheduledTaskAction -Execute $pyw -Argument "`"$PkgDir\platform\review_server.py`" --config `"$cfgPath`" --serve" -WorkingDirectory $PkgDir
  $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
  Register-ScheduledTask -TaskName "InspReviewServer" -Action $a -Trigger $t -Settings $s -Force | Out-Null
  Write-Host "OK  已登记 InspReviewServer：登录时启动评价页服务"
}
$env:PYTHONUTF8 = "1"
Set-Location $PkgDir
& $py platform\review_server.py --config $cfgPath --open
