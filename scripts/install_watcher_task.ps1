[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = 'AI-Wiki-Obsidian-Watcher',
    [string]$VaultPath,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($PythonPath) {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "Python executable not found: $PythonPath"
    }
    $python = (Resolve-Path -LiteralPath $PythonPath).Path
} else {
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw "Venv Python not found: $python"
}

if (-not $VaultPath) { $VaultPath = $env:OBSIDIAN_VAULT_PATH }
if (-not $VaultPath) {
    $obsidianConfig = Join-Path $env:APPDATA 'Obsidian\obsidian.json'
    if (Test-Path -LiteralPath $obsidianConfig) {
        $vaults = (Get-Content -LiteralPath $obsidianConfig -Raw -Encoding UTF8 | ConvertFrom-Json).vaults
        $openVault = @($vaults.PSObject.Properties | ForEach-Object { $_.Value } | Where-Object { $_.open }) | Select-Object -First 1
        if ($openVault) { $VaultPath = [string]$openVault.path }
    }
}
if (-not $VaultPath -or -not (Test-Path -LiteralPath $VaultPath -PathType Container)) {
    throw 'No existing Obsidian vault found. Pass -VaultPath or set OBSIDIAN_VAULT_PATH.'
}
$VaultPath = (Resolve-Path -LiteralPath $VaultPath).Path

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
$escapedSid = [System.Security.SecurityElement]::Escape($sid)
$escapedPython = [System.Security.SecurityElement]::Escape($python)
$escapedRoot = [System.Security.SecurityElement]::Escape($repoRoot)
$escapedVault = [System.Security.SecurityElement]::Escape($VaultPath)

# Explicit XML avoids cmdlet defaults that can set StopOnIdleEnd=true.
$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>AI Wiki Obsidian watcher; direct venv Python action.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>$escapedSid</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>$escapedSid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>false</WakeToRun><Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>$escapedPython</Command>
    <Arguments>-m engine.watcher --vault &quot;$escapedVault&quot;</Arguments>
    <WorkingDirectory>$escapedRoot</WorkingDirectory>
  </Exec></Actions>
</Task>
"@

[xml]$validatedXml = $taskXml
$ns = [System.Xml.XmlNamespaceManager]::new($validatedXml.NameTable)
$ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
if ($validatedXml.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:Command', $ns).InnerText -ne $python -or
    $validatedXml.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:WorkingDirectory', $ns).InnerText -ne $repoRoot -or
    $validatedXml.SelectSingleNode('/t:Task/t:Actions/t:Exec/t:Arguments', $ns).InnerText -notmatch '^\-m engine\.watcher --vault "') {
    throw 'Generated Scheduled Task XML failed local validation.'
}

if ($PSCmdlet.ShouldProcess($TaskName, 'Register or replace Scheduled Task')) {
    Register-ScheduledTask -TaskName $TaskName -Xml $taskXml -Force | Out-Null
    Write-Output "Registered $TaskName for $($identity.Name) using $python (cwd $repoRoot; vault $VaultPath)."
}
