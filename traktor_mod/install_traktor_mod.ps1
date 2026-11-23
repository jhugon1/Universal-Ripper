<#
 Installs or removes the Ripper's Traktor deck-sync mod: the "traktor-api-client" mapping by Erik Minekus (MIT, see
 LICENSE-traktor-api-client.txt). Traktor then tells the Ripper which track is on which deck.

   Install Traktor Mod.bat            install
   Remove Traktor Mod.bat             remove it again

 Or from PowerShell:  .\install_traktor_mod.ps1 [-Uninstall]
 It needs administrator rights (Traktor lives in Program Files) and asks Windows for them itself. Close Traktor first.

 What it changes: ONE file, <Traktor>\Resources64\qml\CSI\D2\D2.qml (two lines are added; your original is saved once to
 %USERPROFILE%\ripper-traktor-backup), plus a new folder next to it, D2\Api. A Traktor update overwrites both: run this again.
 -TraktorDir points it at another folder (tests); -NoElevate and -NoPause are for tests too.
#>
param([switch]$Uninstall, [string]$TraktorDir = '', [switch]$NoElevate, [switch]$NoPause)
$ErrorActionPreference = 'Stop'

$pinned = @{
    'ApiChannel.qml' = '63bf4c6d6290e6ed5b02bbd65c0cb1570d4dc30519ea069b4409502e3f0182ae'
    'ApiClient.js' = 'd41c1f7fdc1c6846b446f0411b75a43e6940d517a42b581eaa466e9da54312f0'
    'ApiDeck.qml' = '22040296173362f5c6e1fad0030827f7107cbcfec120199fe74da802b07d50b6'
    'ApiMasterClock.qml' = '7b126fe9bbab01a9ce102012996f7d34d3bf1664e7f633850b134b85d6050c95'
    'ApiModule.qml' = '8e9f090c6327684106f5a35769a6207198ecde0afbdf8d216f5719628e84087c'
}

function Get-Sha256($path) {
  $sha = [Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($path))) -replace '-', '').ToLower() } finally { $sha.Dispose() }
}

function Test-Admin {
  ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not $NoElevate -and -not (Test-Admin)) {
  $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath))
  if ($Uninstall) { $argList += '-Uninstall' }
  if ($TraktorDir) { $argList += @('-TraktorDir', ('"{0}"' -f $TraktorDir)) }
  Write-Host 'Windows will ask for permission (the Traktor files are in Program Files)...'
  Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
  exit 0
}

function Finish($code) {
  if (-not $NoPause) { Write-Host ''; [void](Read-Host 'Press Enter to close this window') }
  exit $code
}
function Fail($message) { Write-Host ''; Write-Host "ERROR: $message" -ForegroundColor Red; Write-Host 'Nothing was changed.'; Finish 1 }

# ---- find Traktor's D2 mapping ----
$candidates = @()
if ($TraktorDir) { $candidates += $TraktorDir }
else {
  foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($root) { foreach ($name in 'Traktor Pro 4', 'Traktor Pro 3') { $candidates += (Join-Path $root "Native Instruments\$name") } }
  }
}
$d2dir = $null
foreach ($c in $candidates) {
  $d = Join-Path $c 'Resources64\qml\CSI\D2'
  if (Test-Path -LiteralPath (Join-Path $d 'D2.qml')) { $d2dir = $d; break }
}
if (-not $d2dir) { Fail 'Could not find Traktor Pro (Resources64\qml\CSI\D2\D2.qml). Is Traktor Pro 3 or 4 installed in Program Files?' }
if (-not $TraktorDir -and (Get-Process -Name 'Traktor Pro 4', 'Traktor Pro 3' -ErrorAction SilentlyContinue)) { Fail 'Traktor is running. Close it, then run this again.' }

$d2 = Join-Path $d2dir 'D2.qml'
$apiDir = Join-Path $d2dir 'Api'
$backupDir = Join-Path $env:USERPROFILE 'ripper-traktor-backup'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$text = [IO.File]::ReadAllText($d2)
$nl = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
$isPatched = $text -match 'ApiModule\s*\{\s*\}'
Write-Host "Traktor mapping: $d2"

if ($Uninstall) {
  if ($isPatched) {
    $text = [regex]::Replace($text, '(?m)^import "\./Api"[^\r\n]*\r?\n', '')
    $text = [regex]::Replace($text, '(?m)^[ \t]*ApiModule\s*\{\s*\}[ \t]*\r?\n(\r?\n)?', '')
    [IO.File]::WriteAllText($d2, $text, $utf8)
    Write-Host 'Removed the two added lines from D2.qml.'
  } else { Write-Host 'D2.qml has no mod lines (nothing to remove there).' }
  foreach ($n in $pinned.Keys) { $f = Join-Path $apiDir $n; if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force } }
  if ((Test-Path -LiteralPath $apiDir) -and -not (Get-ChildItem -LiteralPath $apiDir -Force)) { Remove-Item -LiteralPath $apiDir -Force }
  Write-Host 'The mod is removed. Your backup of the original stays in' $backupDir
  Finish 0
}

# ---- install ----
if (-not $isPatched) {
  $reImport = [regex]'(?m)^import "\.\./Common/Settings"[^\r\n]*\r?\n'
  $reMapping = [regex]'(?m)^Mapping[ \t]*\r?\n\{[^\r\n]*\r?\n'
  if (-not $reImport.IsMatch($text) -or -not $reMapping.IsMatch($text)) {
    Fail 'This Traktor version looks different from the one the mod was written for (D2.qml has no expected spot for the two lines).'
  }
  New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
  $backup = Join-Path $backupDir 'D2.qml.orig'
  if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $d2 -Destination $backup; Write-Host "Saved your original D2.qml to $backup" }
  $text = $reImport.Replace($text, { param($m) $m.Value + 'import "./Api"' + $nl }, 1)
  $text = $reMapping.Replace($text, { param($m) $m.Value + '  ApiModule {}' + $nl + $nl }, 1)
  [IO.File]::WriteAllText($d2, $text, $utf8)
  Write-Host 'Added the two lines to D2.qml.'
} else { Write-Host 'D2.qml already has the mod lines.' }

New-Item -ItemType Directory -Force -Path $apiDir | Out-Null
$source = Join-Path $PSScriptRoot 'Api'
foreach ($n in $pinned.Keys) {
  $from = Join-Path $source $n
  if (-not (Test-Path -LiteralPath $from)) { Fail "The bundled file $n is missing from the traktor_mod\Api folder." }
  if ((Get-Sha256 $from) -ne $pinned[$n]) { Fail "The bundled file $n is not the expected version (it was changed or damaged)." }
  Copy-Item -LiteralPath $from -Destination (Join-Path $apiDir $n) -Force
}
Write-Host "Copied the $($pinned.Count) mod files to $apiDir"
Write-Host ''
Write-Host 'Last step, once, inside Traktor: Preferences > Controller Manager > Add... and add the device "Kontrol D2"'
Write-Host '(you do not need the hardware). Traktor then reports its decks to the Ripper while the Ripper is running.'
Finish 0
