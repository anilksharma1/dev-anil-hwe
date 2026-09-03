<#
.SYNOPSIS
    Resilient supervisor for worker.py on the Windows leg.

.DESCRIPTION
    Gap: worker.py is a hard dependency on this one VM staying up -- if the Python process
    crashes (an unhandled exception, a COM/Office hang that eventually kills it, an OOM, a
    transient network blip that bubbles up as an unexpected exit), nothing restarts it, and
    every legacy .doc/.xls/.ppt file in the corpus silently stalls until an operator notices
    and manually re-runs `python worker.py`. The Linux side doesn't have this problem --
    Azure Container Apps restarts a crashed container on its own; this script is the
    equivalent for the one leg that runs on a bare VM instead.

    Loops running `python <Command>` (default: worker.py) forever. On ANY exit -- clean or
    not -- it restarts after a backoff, so a crash is downtime measured in seconds, not
    "however long until someone notices". Backoff grows on rapid, repeated failures (a
    genuinely broken config crash-looping every few seconds shouldn't hammer Azure or spam
    logs) and resets once the process has run for a while (a real transient blip should
    recover at full speed, not stay throttled from an unrelated failure hours ago).

    This script does NOT survive a VM reboot by itself -- for that, register it as a
    Scheduled Task with an AtStartup trigger (see register_worker_task.ps1), so the worker
    comes back up automatically after a VM restart/patch cycle too, not just a process crash.

.PARAMETER Command
    The Python script to run each loop (default: worker.py, in this script's own directory).

.PARAMETER InitialBackoffSeconds
    Delay before the first restart after a failure (default: 5).

.PARAMETER MaxBackoffSeconds
    Backoff cap -- doubles on each rapid failure up to this (default: 300 = 5 minutes).

.PARAMETER HealthyRunSeconds
    A run lasting at least this long is treated as "it was working", resetting backoff to
    InitialBackoffSeconds (default: 60).

.PARAMETER MaxRestarts
    Stop after this many restarts instead of looping forever. Omit (or 0) to run forever --
    this exists so the restart/backoff logic itself can be exercised and verified without
    waiting on a real, indefinitely-running worker process.

.EXAMPLE
    .\run_worker_forever.ps1
    .\run_worker_forever.ps1 -Command worker.py -MaxBackoffSeconds 600
#>
param(
    [string]$Command = "worker.py",
    [int]$InitialBackoffSeconds = 5,
    [int]$MaxBackoffSeconds = 300,
    [int]$HealthyRunSeconds = 60,
    [int]$MaxRestarts = 0
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogPath = Join-Path $ScriptDir "worker_supervisor.log"

function Write-SupervisorLog([string]$Message) {
    $line = "[{0:yyyy-MM-ddTHH:mm:ss}] {1}" -f (Get-Date), $Message
    Write-Output $line
    Add-Content -Path $LogPath -Value $line -Encoding utf8
}

$backoff = $InitialBackoffSeconds
$restarts = 0

Write-SupervisorLog "supervisor starting -- command='$Command' initial_backoff=${InitialBackoffSeconds}s max_backoff=${MaxBackoffSeconds}s"

while ($true) {
    $start = Get-Date
    Write-SupervisorLog "launching: python $Command"
    & python $Command
    $exitCode = $LASTEXITCODE
    $ranFor = ((Get-Date) - $start).TotalSeconds

    Write-SupervisorLog ("worker exited (code={0}) after {1:N0}s" -f $exitCode, $ranFor)

    if ($ranFor -ge $HealthyRunSeconds) {
        # it was genuinely up and running for a while -- a fresh problem, not a crash loop
        $backoff = $InitialBackoffSeconds
    } else {
        $backoff = [Math]::Min($backoff * 2, $MaxBackoffSeconds)
    }

    $restarts++
    if ($MaxRestarts -gt 0 -and $restarts -ge $MaxRestarts) {
        Write-SupervisorLog "reached MaxRestarts=$MaxRestarts -- stopping (testing mode)"
        exit 0   # an intentional stop, not a failure -- don't leak the last worker exit code
    }

    Write-SupervisorLog "restarting in ${backoff}s (restart #$restarts)..."
    Start-Sleep -Seconds $backoff
}
