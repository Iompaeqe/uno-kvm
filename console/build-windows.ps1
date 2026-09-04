# Build the single-file Windows console app: dist\UnoKVM.exe
# Copy that one file to the Windows console laptop ; nothing else to install.
#
#   .\build-windows.ps1              # needs Python 3.12 + `pip install -r requirements.txt pyinstaller`
#
# The .exe keeps a console window so serial/video messages stay visible.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
if (-not $py -or $py -like "*WindowsApps*") { $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" }
& $py -m PyInstaller --noconfirm --onefile --name UnoKVM `
  --distpath (Join-Path $here "dist") --workpath (Join-Path $here "build") --specpath (Join-Path $here "build") `
  (Join-Path $here "kvm_console.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
Get-Item (Join-Path $here "dist\UnoKVM.exe") | Select-Object FullName, @{n="MB";e={[math]::Round($_.Length/1MB,1)}}
