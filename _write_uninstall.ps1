param([string]$Dest)
$ErrorActionPreference = 'Stop'
if (-not $Dest) { $Dest = Join-Path $env:LOCALAPPDATA 'TravelFrogGrabber' }

# Pure-ASCII script: Chinese strings are inlined as UTF-8 Base64 so that
# PowerShell 5.1 never has to parse non-ASCII bytes as ANSI.
function U([string]$b64) {
  [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64))
}
$displayName = U '5peF6KGM6Z2S6JuZ5a2Y5qGj5oqT5Y+W5Zmo'
$publisher   = U '5pys5Zyw5bel5YW3'
$uninstaller = U '5Y246L29'

$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TravelFrogGrabber'
if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }

New-ItemProperty -Path $key -Name DisplayName     -Value $displayName -PropertyType String -Force | Out-Null
New-ItemProperty -Path $key -Name DisplayVersion  -Value '1.0'         -PropertyType String -Force | Out-Null
New-ItemProperty -Path $key -Name Publisher       -Value $publisher    -PropertyType String -Force | Out-Null
New-ItemProperty -Path $key -Name InstallLocation -Value $Dest         -PropertyType String -Force | Out-Null
New-ItemProperty -Path $key -Name DisplayIcon     -Value (Join-Path $Dest 'icon.ico') -PropertyType String -Force | Out-Null
New-ItemProperty -Path $key -Name UninstallString -Value ('"' + (Join-Path $Dest ($uninstaller + '.bat')) + '"') -PropertyType String -Force | Out-Null
New-ItemProperty -Path $key -Name NoModify        -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $key -Name NoRepair        -Value 1 -PropertyType DWord -Force | Out-Null

Write-Output 'OK'
