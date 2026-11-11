# Windows PowerShell 5.1 / PowerShell 7. Run using the existing AQE Python venv.
[CmdletBinding()]
param(
    [string]$ProjectRoot = "C:\vscode\aqe",
    [string]$Config = "configs\local.yaml",
    [string]$Instrument = "BTCUSDT",
    [ValidateRange(0.1, 1000000)][double]$MinFreeGB = 20,
    [ValidateRange(1, 3600)][int]$PollSeconds = 30,
    [ValidateRange(0, 86400)][int]$DurationSeconds = 0,
    [switch]$Check,
    [switch]$Status,
    [switch]$Watch
)
$ErrorActionPreference = "Stop"
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$launcher = Join-Path $PSScriptRoot "collect_local.py"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "AQE Python not found: $python. Set -ProjectRoot to your AQE directory."
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Keep start_collect.ps1 and collect_local.py in the same folder."
}
$pythonArgs = @("-u", $launcher, "--project-root", $ProjectRoot, "--config", $Config,
    "--instrument", $Instrument, "--min-free-gb", $MinFreeGB.ToString([System.Globalization.CultureInfo]::InvariantCulture),
    "--poll-seconds", [string]$PollSeconds)
if ($Check) { $pythonArgs += "--check" }
if ($Status) { $pythonArgs += "--status" }
if ($Watch) { $pythonArgs += "--watch" }
if ($DurationSeconds -gt 0) { $pythonArgs += @("--duration-seconds", [string]$DurationSeconds) }
& $python @pythonArgs
exit $LASTEXITCODE
