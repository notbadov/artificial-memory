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
