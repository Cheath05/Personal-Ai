# Run Ollama as Cardinal's G14 brain from boot, before anyone logs in, and restart it if it stops.
# The Ollama tray app is unreliable at starting its own server once OLLAMA_HOST is set, so this task owns the server;
# the tray app can still run for updates and just talks to it.
# Run once in an admin PowerShell: .\scripts\g14-ollama-task.ps1   (re-run any time; it replaces the task)
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$TaskName = 'Cardinal Ollama'
$Ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
$Models = Join-Path $env:USERPROFILE '.ollama\models'
if (-not (Test-Path $Ollama)) { throw "Ollama not found at $Ollama. Install it first: winget install Ollama.Ollama" }

# Listen on every interface; the Tailscale firewall rule (100.64.0.0/10) decides who gets in.
$cmd = "set OLLAMA_HOST=0.0.0.0&& set OLLAMA_MODELS=$Models&& `"$Ollama`" serve"
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/d /c $cmd"
$atBoot = New-ScheduledTaskTrigger -AtStartup
# Watchdog: try again every 5 minutes. If Ollama is still running, IgnoreNew makes this a no-op.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
# S4U: runs as you without storing your password, with no console window.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $atBoot, $watchdog -Principal $principal -Settings $settings `
  -Description 'Ollama server for Cardinal (reachable over Tailscale).' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
"Registered and started '$TaskName'."
