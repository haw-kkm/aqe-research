# Generic restart-with-backoff supervisor for long-running AQE commands (native Windows path; no Docker).
# Usage: powershell -ExecutionPolicy Bypass -File deploy\windows\supervise.ps1 -Command collect -Config configs\local.yaml
param(
  [Parameter(Mandatory=$true)][ValidateSet("collect","context-worker")][string]$Command,
  [string]$Config = "configs\local.yaml",
  [int]$MaxBackoffSeconds = 300
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$aqe = Join-Path $root ".venv\Scripts\aqe.exe"
$backoff = 5
while ($true) {
  $start = Get-Date
  Write-Output ("{0} starting aqe {1}" -f (Get-Date -Format o), $Command)
  & $aqe $Command --config $Config
  $code = $LASTEXITCODE
  $ran = ((Get-Date) - $start).TotalSeconds
  Write-Output ("{0} aqe {1} exited code={2} after {3:N0}s" -f (Get-Date -Format o), $Command, $code, $ran)
  if ($ran -gt 600) { $backoff = 5 } else { $backoff = [Math]::Min($backoff * 2, $MaxBackoffSeconds) }
  Start-Sleep -Seconds $backoff
}
