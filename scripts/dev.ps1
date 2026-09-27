# Run Cardinal locally: API + web app on http://localhost:8000
# Pass -Lan to let your phone on the same Wi-Fi reach it (no login yet, so only on a network you trust).
param([switch]$Lan)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..\services\api')
$HostAddr = if ($Lan) { '0.0.0.0' } else { '127.0.0.1' }
uv run uvicorn cardinal.main:app --reload --host $HostAddr --port 8000 --reload-dir cardinal --reload-dir config
