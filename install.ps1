# ScanAssistant installer (Windows).
#
# Usage (PowerShell):
#   irm https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.ps1 | iex
#   .\install.ps1 [-TargetDir <path>]   # default: %USERPROFILE%\ScanAssistant

param(
    [string]$TargetDir = (Join-Path $env:USERPROFILE "ScanAssistant")
)

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/murypaul/ScanAssistant.git"
$ArchiveUrl = "https://github.com/murypaul/ScanAssistant/archive/refs/heads/master.zip"

Write-Host "== ScanAssistant installer =="

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python was not found. Install Python 3.11+ from https://www.python.org/downloads/ (check 'Add python.exe to PATH')."
    exit 1
}

$versionOk = & python -c "import sys; print(1 if sys.version_info >= (3, 11) else 0)"
if ($versionOk -ne "1") {
    $found = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
    Write-Error "Python 3.11+ is required (found $found)."
    exit 1
}

if (Test-Path (Join-Path $TargetDir ".git")) {
    Write-Host "Existing installation found in $TargetDir - updating..."
    git -C $TargetDir pull --ff-only
} elseif (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "Cloning ScanAssistant into $TargetDir..."
    git clone --depth 1 $RepoUrl $TargetDir
} else {
    Write-Host "git not found - downloading a source archive instead..."
    $parent = Split-Path $TargetDir -Parent
    $zip = Join-Path $env:TEMP "scanassistant-download.zip"
    Invoke-WebRequest -Uri $ArchiveUrl -OutFile $zip
    $extractDir = Join-Path $env:TEMP "scanassistant-extract"
    if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $extractDir -Force
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if (Test-Path $TargetDir) { Remove-Item $TargetDir -Recurse -Force }
    Move-Item -Path (Join-Path $extractDir "ScanAssistant-master") -Destination $TargetDir
    Remove-Item $zip -Force
    Remove-Item $extractDir -Recurse -Force
}

if (-not (Get-Command exiftool -ErrorAction SilentlyContinue)) {
    Write-Host "Note: exiftool was not found - metadata will be skipped with a warning" -ForegroundColor Yellow
    Write-Host "      until installed: https://exiftool.org (download the Windows executable)." -ForegroundColor Yellow
}

Write-Host "Setting up and launching ScanAssistant..."
& (Join-Path $TargetDir "run.bat")
