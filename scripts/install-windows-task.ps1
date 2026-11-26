# Registers a Windows Task Scheduler task that starts the Nakama scheduler at logon (no Docker needed).
# Usage (PowerShell, from repo root):  .\scripts\install-windows-task.ps1
# Remove:                              Unregister-ScheduledTask -TaskName Nakama -Confirm:$false
param(
    [string]$TaskName = "Nakama"
)
$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"   # pythonw = no console window
if (-not (Test-Path $pythonw)) {
    throw "Not found: $pythonw. Create the venv first: python -m venv .venv; .\.venv\Scripts\pip install -e ."
}

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "-m nakama serve --ui --open" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Restart on failure; allow running on battery; never time out (it is a daemon).
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Nakama agent fleet scheduler" -Force | Out-Null
Write-Host "Registered task '$TaskName'. Start now with: Start-ScheduledTask -TaskName $TaskName"
