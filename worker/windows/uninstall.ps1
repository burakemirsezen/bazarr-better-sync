<#
.SYNOPSIS
  Stops the Bazarr Sync tray app and removes start at login. -RemoveFiles also deletes
  %LOCALAPPDATA%\BazarrSyncWorker (environment, models, logs); -RemoveToken clears BBS_WORKER_TOKEN.
#>
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'BazarrSyncWorker'),
  [switch]$RemoveFiles,
  [switch]$RemoveToken
)
$ErrorActionPreference = 'Stop'
Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$InstallDir*" } | Stop-Process -Force
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'BazarrSyncWorker' -ErrorAction SilentlyContinue
if ($RemoveToken) { [Environment]::SetEnvironmentVariable('BBS_WORKER_TOKEN', $null, 'User') }
if ($RemoveFiles -and (Test-Path $InstallDir)) { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
Write-Host 'Bazarr Sync worker removed from startup.' -ForegroundColor Green
