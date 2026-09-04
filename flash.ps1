<#
.SYNOPSIS
  Flash the Uno KVM firmware onto an Arduino Uno R3 (genuine, ATmega16U2 USB chip).

.DESCRIPTION
  Three steps, run in order, Uno plugged into THIS PC:

    .\flash.ps1 install -Port COM4    # 1. put the HoodLoader2 installation sketch on the 328P
                                       #    (then wire 4 jumpers + cap, replug: see README)
    .\flash.ps1 bridge  -Port COM4    # 2. 328P: CH340 <-> 16U2 byte bridge   (board "HoodLoader2 Uno")
    .\flash.ps1 hid     -Port COM4    # 3. 16U2: USB keyboard+mouse firmware  (board "HoodLoader2 16u2")

  The port can change between steps (HoodLoader2 enumerates as a new device) - run
  `.\flash.ps1 ports` to see what is plugged in.

  Requires arduino-cli (winget install ArduinoSA.CLI), the HoodLoader2 core and the
  HID-Project library. `.\flash.ps1 setup` installs the latter two.
#>
param(
  [Parameter(Position = 0)][ValidateSet("setup", "ports", "install", "bridge", "hid", "compile")]
  [string]$Step = "ports",
  [string]$Port
)
$ErrorActionPreference = "Continue"   # arduino-cli prints harmless notes on stderr; exit codes are checked instead
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$fw = Join-Path $here "firmware"
$hl2Url = "https://raw.githubusercontent.com/NicoHood/HoodLoader2/master/package_NicoHood_HoodLoader2_index.json"

# HoodLoader2 2.0.5 pins avr-gcc 4.8, which cannot build the modern Arduino core; borrow the
# 7.3 toolchain that the stock arduino:avr core installs.
$gcc = Get-ChildItem "$env:LOCALAPPDATA\Arduino15\packages\arduino\tools\avr-gcc" -Directory |
  Where-Object Name -like "7.*" | Select-Object -First 1
if (-not $gcc -and $Step -ne "setup") { throw "avr-gcc 7.x not found - run .\flash.ps1 setup" }
$gccProp = if ($gcc) { "runtime.tools.avr-gcc.path=$($gcc.FullName)" } else { "" }

function Need-Port { if (-not $Port) { throw "-Port COMx is required (see .\flash.ps1 ports)" } }
function Invoke-Cli { & arduino-cli @args; if ($LASTEXITCODE -ne 0) { throw "arduino-cli $($args[0]) failed (exit $LASTEXITCODE)" } }

switch ($Step) {
  "setup" {
    Invoke-Cli config add board_manager.additional_urls $hl2Url
    Invoke-Cli core update-index
    Invoke-Cli core install arduino:avr
    Invoke-Cli core install HoodLoader2:avr
    Invoke-Cli lib install HID-Project
  }
  "ports" {
    Invoke-Cli board list
  }
  "compile" {
    Invoke-Cli compile -b HoodLoader2:avr:unoHIDBridge --build-property $gccProp (Join-Path $fw "kvm_bridge_328p")
    Invoke-Cli compile -b HoodLoader2:avr:HoodLoader2atmega16u2 --build-property $gccProp (Join-Path $fw "kvm_hid_16u2")
  }
  "install" {
    Need-Port
    $pkg = Get-ChildItem "$env:LOCALAPPDATA\Arduino15\packages\HoodLoader2\hardware\avr" -Directory | Select-Object -First 1
    $sketch = Join-Path $pkg.FullName "examples\Installation_Sketch"
    if (-not (Test-Path $sketch)) { throw "Installation_Sketch not found under $($pkg.FullName)" }
    # Stock Uno at this point: normal 16U2 USB-serial, board arduino:avr:uno.
    Invoke-Cli compile -b arduino:avr:uno $sketch
    Invoke-Cli upload  -b arduino:avr:uno -p $Port $sketch
    Write-Host ""
    Write-Host "Uploaded. Now UNPLUG the Uno, wire D10/D11/D12/D13 + the 100nF cap as in README.md, replug,"
    Write-Host "and wait ~40 s: slow blink = programming, fast blink (10 Hz) = HoodLoader2 installed."
  }
  "bridge" {
    Need-Port
    $s = Join-Path $fw "kvm_bridge_328p"
    Invoke-Cli compile -b HoodLoader2:avr:unoHIDBridge --build-property $gccProp $s
    Invoke-Cli upload  -b HoodLoader2:avr:unoHIDBridge -p $Port $s
  }
  "hid" {
    Need-Port
    $s = Join-Path $fw "kvm_hid_16u2"
    # CDC_DISABLED: the 16U2 has 4 USB endpoints; a serial interface would take 3 and leave room for
    # only ONE HID interface (mouse OR keyboard). Without it both boot-protocol devices fit.
    # --clean because arduino-cli's core cache ignores the extra flag otherwise.
    Invoke-Cli compile --clean -b HoodLoader2:avr:HoodLoader2atmega16u2 --build-property $gccProp --build-property "compiler.cpp.extra_flags=-DCDC_DISABLED" $s
    Invoke-Cli upload  -b HoodLoader2:avr:HoodLoader2atmega16u2 -p $Port $s
    Write-Host ""
    Write-Host "Done. Windows should now show an 'HID Keyboard Device' + 'HID-compliant mouse' and NO COM port."
    Write-Host "To re-flash later: put the 16U2 back into its bootloader first (README, 'Re-flashing'), then"
    Write-Host "run this step against the 'HoodLoader2 Uno' port that appears."
  }
}
