# Installs (or removes, with -Remove) the waveform experiment into Traktor's D2 mod. Run in an ADMINISTRATOR PowerShell
# with Traktor closed:   powershell -ExecutionPolicy Bypass -File traktor_mod\install_waveform.ps1   [-Remove]
param([switch]$Remove)
$api = 'C:\Program Files\Native Instruments\Traktor Pro 4\Resources64\qml\CSI\D2\Api'
$module = Join-Path $api 'ApiModule.qml'
$backup = Join-Path $api 'ApiModule.qml.before-waveform'
$view = Join-Path $api 'ApiWaveformView.qml'
if (Get-Process Traktor -ErrorAction SilentlyContinue) { throw 'Close Traktor first.' }
if ($Remove) {
  if (Test-Path $backup) { Copy-Item $backup $module -Force; Remove-Item $backup }
  Remove-Item $view -ErrorAction SilentlyContinue
  'Removed: the mod is back to how it was.'; return
}
if (-not (Test-Path $backup)) { Copy-Item $module $backup }   # keep the FIRST original
$waveDir = (Join-Path $env:LOCALAPPDATA 'RippedRipper\waveforms') -replace '\', '/'
New-Item -ItemType Directory -Force -Path ($waveDir -replace '/', '\') | Out-Null
(Get-Content (Join-Path $PSScriptRoot 'ApiWaveformView.qml') -Raw).Replace('@WAVE_DIR@', $waveDir) | Set-Content $view -Encoding UTF8
$text = Get-Content $backup -Raw
if ($text -notmatch 'ApiWaveformView') {
  $loaders = (0..3 | ForEach-Object { "  Loader { source: `"ApiWaveformView.qml`"; onLoaded: item.deckId = $_ }" }) -join "`r`n"
  $text = $text -replace '(?m)^(\s*ApiMasterClock \{\}\s*)$', "`$1`r`n$loaders"
}
Set-Content $module $text -Encoding UTF8
"Installed. Start Traktor, load a track, then check: $waveDir\deckA.png"
"Undo any time with: -Remove"
