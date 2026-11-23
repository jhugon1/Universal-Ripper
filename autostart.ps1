<#
 Makes the Ripper start by itself whenever Traktor opens, and quit again a little after Traktor closes.
   Turn on auto-start.bat     (or: powershell -ExecutionPolicy Bypass -File autostart.ps1)
   Turn off auto-start.bat    (or: ... -Remove)
 It puts a small shortcut in your Windows Startup folder; that runs traktor_watcher.py (no window, almost no CPU) every time you sign
 in. It also starts the watcher right now, so there is no need to sign out first.
   -NoStart       do not start the watcher now (tests)
   -StartupDir    use this folder instead of the Windows Startup folder (tests)
#>
param([switch]$Remove, [switch]$NoStart, [string]$StartupDir = '')
Set-Location -LiteralPath $PSScriptRoot
$startup = if ($StartupDir) { $StartupDir } else { [Environment]::GetFolderPath('Startup') }
$link = Join-Path $startup 'Ripped Ripper (starts with Traktor).lnk'
$pythonw = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$watcher = Join-Path $PSScriptRoot 'traktor_watcher.py'

function Stop-Watcher {
  Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*traktor_watcher.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Remove) {
  if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }
  if (-not $NoStart) { Stop-Watcher }
  Write-Host 'Auto-start is off: the Ripper no longer starts with Traktor. Start it with the Desktop icon whenever you want it.'
  Write-Host '(A Ripper that is running right now carries on until Traktor closes.)'
  exit 0
}

if (-not (Test-Path -LiteralPath $pythonw)) { Write-Host 'Run Setup.bat first: the Ripper is not set up yet.' -ForegroundColor Red; exit 1 }
New-Item -ItemType Directory -Force -Path $startup | Out-Null
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $pythonw
$sc.Arguments = '"' + $watcher + '"'
$sc.WorkingDirectory = $PSScriptRoot
$sc.WindowStyle = 7
$icon = Join-Path $env:LOCALAPPDATA 'RippedRipper\ripper.ico'
if (Test-Path -LiteralPath $icon) { $sc.IconLocation = $icon }
$sc.Description = 'Starts the Ripper when Traktor opens'
$sc.Save()
if (-not $NoStart) { Start-Process -FilePath $pythonw -ArgumentList ('"' + $watcher + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden }
Write-Host 'Auto-start is on: the Ripper now starts by itself when Traktor opens (and quits a little after Traktor closes).'
Write-Host "Shortcut: $link"
exit 0
