# Windows (G14): install ActivityWatch (free, open source) and send app usage to Cardinal every 15 minutes.
# Get the secret first: Cardinal → Review → Focus → "Connect a laptop". Run in PowerShell (not as admin):
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-aw-bridge.ps1
$ErrorActionPreference = 'Stop'
$Hub = if ($env:CARDINAL_HUB) { $env:CARDINAL_HUB } else { 'https://cardinal.tailaf3b0c.ts.net' }
$Device = if ($env:CARDINAL_DEVICE) { $env:CARDINAL_DEVICE } else { 'G14' }
$Dir = Join-Path $env:LOCALAPPDATA 'Cardinal'
$Uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'

if (-not (Get-Command aw-qt -ErrorAction SilentlyContinue) -and -not (Test-Path "$env:LOCALAPPDATA\Programs\ActivityWatch")) {
  Write-Host 'Installing ActivityWatch...'
  winget install --id ActivityWatch.ActivityWatch -e --accept-source-agreements --accept-package-agreements
}
Write-Host 'Start ActivityWatch once from the Start menu so it runs at sign-in.'

$Secure = Read-Host 'Paste the secret from Cardinal' -AsSecureString
$Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure))
if (-not $Token) { throw 'No secret entered.' }
@{ hub = $Hub; token = $Token; device = $Device } | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $env:USERPROFILE '.cardinal-aw.json')

New-Item -ItemType Directory -Force $Dir | Out-Null
Copy-Item (Join-Path $PSScriptRoot 'aw_bridge.py') (Join-Path $Dir 'aw_bridge.py') -Force
$Action = New-ScheduledTaskAction -Execute $Uv -Argument "run --no-project --python 3.12 `"$Dir\aw_bridge.py`""
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -Hidden
Register-ScheduledTask -TaskName 'Cardinal App Usage' -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
Start-ScheduledTask -TaskName 'Cardinal App Usage'
Write-Host 'Done. This PC now sends app usage every 15 minutes (task: Cardinal App Usage).'
