<#
.SYNOPSIS
  Installs the Bazarr Sync GPU worker for the current Windows user and starts its tray app.

.DESCRIPTION
  Everything goes under %LOCALAPPDATA%\BazarrSyncWorker (no admin rights):
    venv\     Python 3.12 environment with the ROCm GPU stack and this package
    models\   speech model cache (downloaded on the first job, ~5.6 GB)
    alass\    alass-cli.exe (section-offset engine)
    config.json, logs\, scratch\
  The worker token is stored only in the user environment variable BBS_WORKER_TOKEN.
  Start at login is a per-user Run registry entry (toggle it from the tray menu).

  Fresh machine: builds the venv from worker\requirements-gpu.txt, which pins AMD's
  *nightly* ROCm wheels. Those may be withdrawn upstream; if pip cannot find them,
  install a current ROCm torch for your GPU and rerun with -ExistingVenv.
  Existing setup: -ExistingVenv / -ExistingModels are MOVED into place; -ExistingAlass is copied.

.EXAMPLE
  .\install.ps1 -Coordinator https://bazarr-sync-worker.tail165123.ts.net -ReviewUrl https://bazarr-sync.bsezen.com
#>
param(
  [Parameter(Mandatory = $true)][string]$Coordinator,
  [string]$ReviewUrl = '',
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'BazarrSyncWorker'),
  [string]$Python = '',
  [string]$ExistingVenv = '',
  [string]$ExistingModels = '',
  [string]$ExistingAlass = '',
  [switch]$NoAutostart,
  [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$venv = Join-Path $InstallDir 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$rocmIndex = 'https://nightly.repo.amd.com/rocm/core/whl-next/'
$alassUrl = 'https://github.com/kaegi/alass/releases/download/v2.0.0/alass-windows64.zip'

function Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Invoke-Checked($exe, [string[]]$arguments) {
  & $exe @arguments
  if ($LASTEXITCODE -ne 0) { throw "$exe failed with exit code $LASTEXITCODE" }
}

Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$venv*" } | ForEach-Object {
  Step 'Stopping the running tray app'
  $_ | Stop-Process -Force
}
New-Item -ItemType Directory -Force -Path $InstallDir, (Join-Path $InstallDir 'logs'), (Join-Path $InstallDir 'scratch') | Out-Null

# --- Python environment -------------------------------------------------------
if ($ExistingVenv) {
  if (Test-Path $venv) { throw "$venv already exists; remove it or omit -ExistingVenv" }
  Step "Moving existing GPU environment from $ExistingVenv"
  Move-Item -LiteralPath $ExistingVenv -Destination $venv
} elseif (-not (Test-Path $venvPython)) {
  Step 'Creating Python 3.12 environment'
  if ($Python) { Invoke-Checked $Python @('-m', 'venv', $venv) } else { Invoke-Checked 'py' @('-3.12', '-m', 'venv', $venv) }
  Step 'Installing the ROCm GPU stack (several GB)'
  Invoke-Checked $venvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
  Invoke-Checked $venvPython @('-m', 'pip', 'install', '--no-deps', '--extra-index-url', $rocmIndex, '-r', (Join-Path $repo 'worker\requirements-gpu.txt'))
}
Step 'Installing the worker package'
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--upgrade', "$repo[worker,tray]")
# Same version number on a rerun would otherwise be skipped; always take this checkout's code.
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--force-reinstall', '--no-deps', $repo)
Invoke-Checked $venvPython @('-c', 'import torch, rapidfuzz, pystray; assert torch.cuda.is_available(), "torch sees no GPU"; print("GPU:", torch.cuda.get_device_name(0))')

# --- Models and ALASS ---------------------------------------------------------
$models = Join-Path $InstallDir 'models'
if ($ExistingModels) {
  if (Test-Path $models) { throw "$models already exists; remove it or omit -ExistingModels" }
  Step "Moving existing model cache from $ExistingModels"
  Move-Item -LiteralPath $ExistingModels -Destination $models
} else {
  New-Item -ItemType Directory -Force -Path $models | Out-Null
}
$alassDir = Join-Path $InstallDir 'alass'
New-Item -ItemType Directory -Force -Path $alassDir | Out-Null
$alass = Join-Path $alassDir 'alass-cli.exe'
if ($ExistingAlass) {
  Step "Copying ALASS from $ExistingAlass"
  Copy-Item -LiteralPath $ExistingAlass -Destination $alass -Force
} elseif (-not (Test-Path $alass)) {
  Step 'Downloading ALASS 2.0.0'
  $zip = Join-Path $env:TEMP 'alass-windows64.zip'
  Invoke-WebRequest -UseBasicParsing -Uri $alassUrl -OutFile $zip
  Write-Host ('    sha256 ' + (Get-FileHash $zip -Algorithm SHA256).Hash)
  $unpacked = Join-Path $env:TEMP 'alass-windows64'
  Expand-Archive -LiteralPath $zip -DestinationPath $unpacked -Force
  Copy-Item (Get-ChildItem $unpacked -Recurse -Filter 'alass-cli.exe' | Select-Object -First 1).FullName $alass
  Remove-Item $zip, $unpacked -Recurse -Force
}

# --- Settings, token, autostart -------------------------------------------------
Step 'Writing config.json'
$json = [ordered]@{ coordinator_url = $Coordinator; review_url = $ReviewUrl; model_cache = $models; alass = $alass;
                    scratch = (Join-Path $InstallDir 'scratch') } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $InstallDir 'config.json'), $json, (New-Object Text.UTF8Encoding $false))

if (-not [Environment]::GetEnvironmentVariable('BBS_WORKER_TOKEN', 'User')) {
  $secure = Read-Host -AsSecureString 'Worker token (hidden)'
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
  if (-not $plain) { throw 'A worker token is required' }
  [Environment]::SetEnvironmentVariable('BBS_WORKER_TOKEN', $plain, 'User')
  Remove-Variable plain
}

$enable = if ($NoAutostart) { 'False' } else { 'True' }
Invoke-Checked $venvPython @('-c', "from bazarr_sync.worker.settings import set_autostart; set_autostart($enable)")
if (-not $NoAutostart) { Step 'Start at login enabled' }

if (-not $NoLaunch) {
  Step 'Starting the tray app'
  Start-Process -FilePath (Join-Path $venv 'Scripts\pythonw.exe') -ArgumentList '-m', 'bazarr_sync.worker.tray'
}
Write-Host "Installed to $InstallDir" -ForegroundColor Green
