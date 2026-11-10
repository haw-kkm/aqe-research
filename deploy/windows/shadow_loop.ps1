# Shadow-mode loop: every 5 minutes top up closed klines via REST, then run `aqe analyze`.
# Reports land in {data_root}/reports; nothing is executed (approval_required=true, execution_enabled=false).
# Requires a config with `mode: shadow` (copy configs\local.yaml -> configs\shadow.yaml and change mode).
param([string]$Config = "configs\shadow.yaml")
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$aqe = Join-Path $root ".venv\Scripts\aqe.exe"
while ($true) {
  & $aqe backfill --config $Config --what klines | Out-Null
  & $aqe analyze --config $Config
  $now = Get-Date
  $next = $now.AddMinutes(5 - ($now.Minute % 5)).AddSeconds(15 - $now.Second)
  Start-Sleep -Seconds ([Math]::Max(5, ($next - (Get-Date)).TotalSeconds))
}
