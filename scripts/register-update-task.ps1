param(
    [string]$TaskName = "Causality Auto Update",
    [string]$Time = "09:00",
    [ValidateSet("Daily", "Weekly")]
    [string]$Schedule = "Weekly",
    [ValidateSet("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")]
    [string]$DayOfWeek = "Sunday",
    [switch]$UpdateCodexCli
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$UpdateScript = Join-Path $PSScriptRoot "update.ps1"
$Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$UpdateScript`" -SkipTests"
if ($UpdateCodexCli) {
    $Arguments += " -UpdateCodexCli"
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $Arguments -WorkingDirectory $ProjectRoot
if ($Schedule -eq "Daily") {
    $Trigger = New-ScheduledTaskTrigger -Daily -At $Time
}
else {
    $Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DayOfWeek -At $Time
}
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Description "Fast-forward update for local Causality harness." -Force | Out-Host
Write-Host "Registered scheduled task '$TaskName' for $Schedule at $Time."
