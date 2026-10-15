$ErrorActionPreference = 'Stop'
# --update-policy manual
$clientRoot = $PSScriptRoot
function Stop-AgentPlatformLauncher([string]$message) {
  $logRoot = Join-Path $clientRoot 'logs'
  New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
  $at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
  Add-Content -LiteralPath (Join-Path $logRoot 'sync-error.log') -Encoding UTF8 -Value ($at + ' ' + $message)
  [Console]::Error.WriteLine($message)
  exit 64
}
$launch = $null
try {
  $launchText = Get-Content -LiteralPath (Join-Path $clientRoot 'sync-client.arguments.json') -Raw -Encoding UTF8
  $launch = ConvertFrom-Json -InputObject $launchText
} catch {
  Stop-AgentPlatformLauncher 'agent-platform: sync-client.arguments.json cannot be read; run a synchronization from the app or the terminal to write it again'
}
if (($null -eq $launch) -or ($launch -is [array]) -or ($launch.version -ne 1) -or (-not ($launch.python -is [string])) -or (-not ($launch.commandLine -is [string])) -or ([string]::IsNullOrWhiteSpace($launch.python)) -or ([string]::IsNullOrWhiteSpace($launch.commandLine))) {
  Stop-AgentPlatformLauncher 'agent-platform: sync-client.arguments.json is not the expected object; run a synchronization from the app or the terminal to write it again'
}
$python = $launch.python
$reinstall = $false
$guard = Join-Path $clientRoot 'sync-client-guard.py'
$launcherEvents = Join-Path $clientRoot 'launcher-events.jsonl'
function Write-AgentPlatformEvent([string]$name) {
  $at = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
  Add-Content -LiteralPath $launcherEvents -Encoding UTF8 -Value ('{"event":"' + $name + '","at":"' + $at + '"}')
}
function Invoke-AgentPlatformGuard {
  $ErrorActionPreference = 'Continue'
  $global:LASTEXITCODE = 70
  & $python -E -s $guard --lock-wait 30 --self-check-limit 60 *> $null
  return $LASTEXITCODE
}
$guardExit = Invoke-AgentPlatformGuard
if ($guardExit -eq 11) { exit 0 }
if (($guardExit -eq 74) -or ($guardExit -eq 75)) { exit $guardExit }
if ($guardExit -eq 10) {
  $reinstall = ($launch.reinstallCommandLine -is [string]) -and (-not [string]::IsNullOrWhiteSpace($launch.reinstallCommandLine))
} elseif ($guardExit -ne 0) {
  Write-AgentPlatformEvent 'guard_failed'
  [Console]::Error.WriteLine('agent-platform: the launch guard did not run; starting the sync client unchecked')
}
$securityModule = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
Import-Module $securityModule -Force
$secure = Get-Content -LiteralPath (Join-Path $clientRoot 'credential.dpapi') | ConvertTo-SecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $env:AGENT_PLATFORM_CLIENT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  $start = New-Object System.Diagnostics.ProcessStartInfo
  $start.FileName = $python
  $start.Arguments = if ($reinstall) { $launch.reinstallCommandLine } else { $launch.commandLine }
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  $process = [System.Diagnostics.Process]::Start($start)
  $process.WaitForExit()
  exit $process.ExitCode
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
  Remove-Item Env:AGENT_PLATFORM_CLIENT_TOKEN -ErrorAction SilentlyContinue
}
