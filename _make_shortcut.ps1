param([string]$Dest)
$ErrorActionPreference = 'Stop'
if (-not $Dest) { $Dest = Join-Path $env:LOCALAPPDATA 'TravelFrogGrabber' }

# Pure-ASCII script: Chinese strings are inlined as UTF-8 Base64 so that
# PowerShell 5.1 never has to parse non-ASCII bytes as ANSI.
function U([string]$b64) {
  [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64))
}
$lnkName = U '5peF6KGM6Z2S6JuZ5oqT5Y+W5Zmo'
$desc    = U '5peF6KGM6Z2S6JuZ5a2Y5qGj5oqT5Y+W5Zmo'

$ws = New-Object -ComObject WScript.Shell
$vbs  = Join-Path $Dest 'launch.vbs'
$vbs2 = Join-Path $Dest 'start.vbs'
$py   = Join-Path $Dest 'runtime\python\python.exe'
$app  = Join-Path $Dest 'app.py'
$icon = Join-Path $Dest 'icon.ico'

function Make-Lnk([string]$path) {
  $s = $ws.CreateShortcut($path)
  if (Test-Path $vbs) {
    $s.TargetPath = $vbs
    $s.Arguments = ''
  } elseif (Test-Path $vbs2) {
    $s.TargetPath = $vbs2
    $s.Arguments = ''
  } else {
    $s.TargetPath = $py
    $s.Arguments = '"' + $app + '"'
  }
  $s.WorkingDirectory = $Dest
  if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
  $s.Description = $desc
  $s.WindowStyle = 7
  $s.Save()
}

# Desktop shortcut
$desktop = [Environment]::GetFolderPath('Desktop')
Make-Lnk (Join-Path $desktop ($lnkName + '.lnk'))

# Start menu shortcut
$sm = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
if (-not (Test-Path $sm)) { New-Item -ItemType Directory -Path $sm -Force | Out-Null }
Make-Lnk (Join-Path $sm ($lnkName + '.lnk'))

Write-Output 'OK'
