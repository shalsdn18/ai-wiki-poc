param([string]$TaskName = 'AI-Wiki-Obsidian-Watcher')

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$expectedPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$taskLog = Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational'
$action = @($task.Actions)[0]
$trigger = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
$proc = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match 'engine\.watcher' -and $_.ExecutablePath -match '\\python(?:310)?\.exe$'
})

$report = [ordered]@{
    TaskName = $task.TaskName
    State = $task.State
    User = $task.Principal.UserId
    LogonType = $task.Principal.LogonType
    ActionExecutable = $action.Execute
    ActionArguments = $action.Arguments
    WorkingDirectory = $action.WorkingDirectory
    AtLogOn = ($trigger.Count -gt 0)
    StartWhenAvailable = $task.Settings.StartWhenAvailable
    ExecutionTimeLimit = $task.Settings.ExecutionTimeLimit
    MultipleInstances = $task.Settings.MultipleInstances
    RunOnlyIfIdle = $task.Settings.RunOnlyIfIdle
    StopOnIdleEnd = $task.Settings.IdleSettings.StopOnIdleEnd
    RestartCount = $task.Settings.RestartCount
    RestartInterval = $task.Settings.RestartInterval
    LastRunTime = $info.LastRunTime
    LastTaskResult = ('0x{0:X8}' -f [uint32]$info.LastTaskResult)
    TaskSchedulerOperationalLogEnabled = $taskLog.IsEnabled
    WatcherPythonProcesses = $proc.Count
    WatcherProcesses = @($proc | ForEach-Object {
        [ordered]@{ ProcessId = $_.ProcessId; ParentProcessId = $_.ParentProcessId; SessionId = $_.SessionId; CommandLine = $_.CommandLine }
    })
}
$report | ConvertTo-Json -Depth 4

$valid = $action.Execute -eq $expectedPython -and
    $action.Arguments -match '^\-m engine\.watcher --vault "[^"]+"$' -and
    $action.WorkingDirectory -eq $repoRoot -and
    $trigger.Count -gt 0 -and
    $task.Settings.StartWhenAvailable -and
    $task.Settings.ExecutionTimeLimit -eq 'PT0S' -and
    $task.Settings.MultipleInstances -eq 'IgnoreNew' -and
    -not $task.Settings.RunOnlyIfIdle -and
    -not $task.Settings.IdleSettings.StopOnIdleEnd -and
    $task.Settings.RestartCount -ge 1 -and
    $task.Settings.RestartInterval -eq 'PT1M' -and
    $task.Principal.LogonType -eq 'Interactive'
if (-not $valid) { throw 'Scheduled Task configuration does not match the expected production settings.' }
