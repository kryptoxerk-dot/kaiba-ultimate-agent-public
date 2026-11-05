param([switch]$Install)

# DESKTOP-INTEL-SYNC-20261103: local public-data task only. The pythonw action
# hides its console; the task itself remains visible in Windows Task Scheduler.
$ErrorActionPreference = 'Stop'
$taskRepoRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRepoRoot '.venv/Scripts/pythonw.exe'
$taskName = 'Kaiba Desktop Public Intel Sync'
$taskArguments = '-m deploy.desktop_intel_sync --collect-and-sync'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    throw 'The Kaiba Python environment is unavailable.'
}
if (-not (Test-Path -LiteralPath (Join-Path $taskRepoRoot 'deploy/desktop_intel_sync.py') -PathType Leaf)) {
    throw 'The reviewed desktop public-intelligence helper is unavailable.'
}
if (-not $Install) {
    [ordered]@{
        task_name = $taskName
        mode = 'preview'
        executable = $taskPython
        arguments = $taskArguments
        working_directory = $taskRepoRoot
        interval_minutes = 5
        maximum_runtime_minutes = 4
        overlap = 'IgnoreNew'
    } | ConvertTo-Json -Compress
    return
}
$taskExisting = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($taskExisting) {
    $taskExistingAction = @($taskExisting.Actions)
    if ($taskExistingAction.Count -ne 1 -or $taskExistingAction[0].Execute -ne $taskPython -or $taskExistingAction[0].Arguments -ne $taskArguments) {
        throw 'A different task already uses this name; leaving it unchanged.'
    }
}
$taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument $taskArguments -WorkingDirectory $taskRepoRoot
$taskTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Minutes 5)
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -RunOnlyIfNetworkAvailable
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Description 'Due-only public ETF, CFTC and market observations, mirrored to the existing Kaiba server with source clocks intact. No orders, paid APIs or credential copying.' -Force | Select-Object TaskName, State
