<#
.SYNOPSIS
    Registers run_worker_forever.ps1 as a Windows Scheduled Task that starts automatically
    at boot -- so the Windows worker leg survives a VM restart/patch cycle, not just a
    process crash (which run_worker_forever.ps1 alone already handles).

.DESCRIPTION
    Gap: worker.py is a hard dependency on this one VM. run_worker_forever.ps1 fixes "the
    Python process crashed"; this script fixes "the VM itself rebooted" -- without it,
    a reboot (Windows Update, a maintenance restart, a power event) leaves the Windows leg
    dead until someone remembers to log in and start it by hand, and every legacy
    .doc/.xls/.ppt file queues up behind it in the meantime.

    Run this ONCE, as Administrator, on the Windows worker VM itself. It is deliberately
    NOT run automatically by anything in this repo -- registering a scheduled task is a
    real, persistent change to the machine, and doing it as a side effect of anything else
    would be a surprise. Read it before running it, the way you would any setup script.

.PARAMETER TaskName
    Scheduled task name (default: "HWE Windows Worker").

.PARAMETER RunAsUser
    Account the task runs as (default: the account running this script). Needs the same
    Azure/az login context worker.py itself needs -- see RUNBOOK.md.

.EXAMPLE
    # As Administrator, from this repo's directory:
    .\register_worker_task.ps1
#>
param(
    [string]$TaskName = "HWE Windows Worker",
    [string]$RunAsUser = "$env:USERDOMAIN\$env:USERNAME"
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SupervisorScript = Join-Path $ScriptDir "run_worker_forever.ps1"

if (-not (Test-Path $SupervisorScript)) {
    Write-Error "run_worker_forever.ps1 not found next to this script ($SupervisorScript) -- aborting."
    exit 1
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$SupervisorScript`"" `
    -WorkingDirectory $ScriptDir

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)   # never time out -- this must run indefinitely

$principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force

Write-Output "Registered scheduled task '$TaskName' -- starts run_worker_forever.ps1 at boot, as $RunAsUser."
Write-Output "Start it now without rebooting:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Output "Check on it:                       Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
