# 移除 v1.0–v1.3 注册的 Windows 计划任务（v1.4 起取消一切定时运行）
# 用法（包根目录的 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File platform\windows\remove_legacy_tasks.ps1            # 先列出，再询问
#   powershell -ExecutionPolicy Bypass -File platform\windows\remove_legacy_tasks.ps1 -Yes       # 不询问直接删
#   ... -KeepReviewServer   保留“登录时启动评价页”（它不是定时运行，只是开机常驻，收货员靠它工作）
# 输入：无；输出：删掉 InspNightly-*、InspWeekly*、InspReview*（以及 -KeepReviewServer 时除外的 InspReviewServer）
param([switch]$Yes, [switch]$KeepReviewServer)
$ErrorActionPreference = "Stop"
$tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "InspNightly*" -or $_.TaskName -like "InspWeekly*" -or $_.TaskName -like "InspReview*" })
if ($KeepReviewServer) { $tasks = @($tasks | Where-Object { $_.TaskName -ne "InspReviewServer" }) }
if ($tasks.Count -eq 0) { Write-Host "OK  没有需要移除的灵感计划任务"; exit 0 }
Write-Host "将移除以下计划任务："
$tasks | ForEach-Object { Write-Host ("  - " + $_.TaskName + "  (" + $_.State + ")") }
if (-not $Yes) {
  $ans = Read-Host "确认移除？输入 y 回车"
  if ($ans -ne "y") { Write-Host "已取消"; exit 1 }
}
foreach ($t in $tasks) {
  try { Stop-ScheduledTask -TaskName $t.TaskName -ErrorAction SilentlyContinue } catch {}
  Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false
  Write-Host ("OK  已移除 " + $t.TaskName)
}
# 评价页服务如果还在跑，提醒一下（不强杀：可能正在写评价）
$srv = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*review_server.py*" }
if ($srv -and -not $KeepReviewServer) { Write-Host "提示：评价页服务仍在运行（PID $($srv.ProcessId -join ',')）。需要时运行 python platform\review_server.py --config config.json --stop" }
Write-Host "完成。之后想开机自动启动评价页（含收货员），运行 platform\windows\start_review_server.ps1 -Autostart"
