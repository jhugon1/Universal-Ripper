<#
 Ripped Ripper - one-time setup for Windows. Double-click Setup.bat (or run: powershell -ExecutionPolicy Bypass -File setup.ps1).
 Safe to run again at any time: it only adds what is missing.
   -Yes           answer "yes" to every question (installs ffmpeg with winget without asking)
   -NoShortcut    do not create the Desktop shortcut
   -SkipFfmpeg    do not look for or install ffmpeg
   -ShortcutDir   put the shortcut in this folder instead of the Desktop
#>
param([switch]$Yes, [switch]$NoShortcut, [switch]$SkipFfmpeg, [string]$ShortcutDir = '')
Set-Location -LiteralPath $PSScriptRoot
$problems = New-Object System.Collections.ArrayList

function Step($t) { Write-Host ''; Write-Host "== $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "   ok  $t" -ForegroundColor Green }
function Warn($t) { Write-Host "   !!  $t" -ForegroundColor Yellow; [void]$problems.Add($t) }
function Stop-Setup($t) { Write-Host ''; Write-Host "   ERROR: $t" -ForegroundColor Red; exit 1 }
function Ask($q) { if ($Yes) { return $true }; $a = Read-Host "   $q [Y/n]"; return ($a -eq '' -or $a -match '^[yY]') }
function Update-SessionPath {
  $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Has-Ffmpeg { return ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) }

# ---- 1. Python 3.10 or newer, with tkinter ----
Step 'Python'
$python = $null
foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
  $exe = $cand[0]
  if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
  $extra = @(); if ($cand.Count -gt 1) { $extra = $cand[1..($cand.Count - 1)] }
  $code = "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')"
  $found = & $exe @extra -c $code 2>$null
  if ($LASTEXITCODE -eq 0 -and $found) { $python = "$found".Trim(); break }
}
if (-not $python) {
  Stop-Setup "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ (on the first screen tick 'Add python.exe to PATH'), then run Setup again."
}
$version = & $python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
Ok "Python $version ($python)"
& $python -c 'import tkinter' 2>$null
if ($LASTEXITCODE -ne 0) {
  Stop-Setup "This Python has no tkinter (the Ripper's button, panel and pop-up need it). Run the Python installer again, choose Modify, and keep 'tcl/tk and IDLE' ticked."
}
Ok 'tkinter is available'

# ---- 2. the private environment and the packages ----
Step 'Packages'
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$venvGood = $false
if (Test-Path -LiteralPath $venvPython) { & $venvPython -c 'import sys' 2>$null; $venvGood = ($LASTEXITCODE -eq 0) }
if (-not $venvGood) {
  Write-Host '   Creating the private Python environment (.venv)...'
  & $python -m venv --clear .venv
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) { Stop-Setup 'Could not create the .venv folder here. Is the folder writable (not read-only or a protected location)?' }
}
Write-Host '   Installing packages (about a minute the first time)...'
& $venvPython -m pip install --disable-pip-version-check --progress-bar off -q --upgrade pip 2>$null | Out-Null
& $venvPython -m pip install --disable-pip-version-check --progress-bar off -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Stop-Setup 'Installing the packages failed. Check your internet connection, then run Setup again.' }
Ok 'packages installed'

# ---- 3. ffmpeg (turns downloads into 320 kbps mp3) ----
Step 'ffmpeg'
if ($SkipFfmpeg) { Write-Host '   skipped' }
else {
  Update-SessionPath
  if (Has-Ffmpeg) { Ok 'ffmpeg and ffprobe found' }
  else {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget -and (Ask 'ffmpeg is needed to convert audio and is not installed. Install it now with winget?')) {
      & winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
      Update-SessionPath
    }
    if (Has-Ffmpeg) { Ok 'ffmpeg installed' }
    elseif ($winget) { Warn 'ffmpeg is still not visible to this window. Close it and run Setup again (or sign out and in once).' }
    else { Warn 'ffmpeg is missing and winget is not available. Install ffmpeg from https://www.gyan.dev/ffmpeg/builds/ (the "essentials" build) and add its bin folder to PATH, then run Setup again.' }
  }
}

# ---- 4. the Desktop shortcut ----
Step 'Shortcut'
if ($NoShortcut) { Write-Host '   skipped' }
else {
  try {
    $dataDir = Join-Path $env:LOCALAPPDATA 'RippedRipper'
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
    $icon = Join-Path $dataDir 'ripper.ico'
    & $venvPython (Join-Path $PSScriptRoot 'tools\make_icon.py') $icon
    $folder = if ($ShortcutDir) { $ShortcutDir } else { [Environment]::GetFolderPath('Desktop') }
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $link = Join-Path $folder 'Ripped Ripper.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($link)
    $sc.TargetPath = (Join-Path $PSScriptRoot 'Start Ripper.bat')
    $sc.WorkingDirectory = $PSScriptRoot
    $sc.WindowStyle = 7
    $sc.IconLocation = $icon
    $sc.Description = 'Ripped Ripper'
    $sc.Save()
    Ok "shortcut: $link"
  } catch { Warn "Could not create the shortcut ($($_.Exception.Message)). You can still start the Ripper with Start Ripper.bat." }
}

# ---- 5. final check ----
Step 'Check'
Update-SessionPath
& $venvPython (Join-Path $PSScriptRoot 'run.py') --check
$checkOk = ($LASTEXITCODE -eq 0)

Write-Host ''
if ($checkOk -and $problems.Count -eq 0) {
  Write-Host 'All set. Start the Ripper with the "Ripped Ripper" icon on your Desktop (or Start Ripper.bat).' -ForegroundColor Green
} else {
  Write-Host 'Setup finished, but something needs attention:' -ForegroundColor Yellow
  foreach ($p in $problems) { Write-Host "   - $p" -ForegroundColor Yellow }
  if (-not $checkOk) { Write-Host '   - see the [!!] lines in the check above' -ForegroundColor Yellow }
}
Write-Host 'Using Traktor? Also run traktor_mod\Install Traktor Mod.bat once (see the README).'
exit 0
