<#
.SYNOPSIS
  Quick wiring test for the KVM input path, no Python needed.
  CH340 plugged into THIS PC, Uno (HID) plugged into the target (can also be this PC).

    .\test-link.ps1                    # ping the 16U2 through CH340 -> 328P bridge -> 16U2
    .\test-link.ps1 -Move              # + wiggle the target's mouse in a small square
    .\test-link.ps1 -Type "hello kvm"  # + type text (lowercase letters, digits, space) after a 5 s countdown
    .\test-link.ps1 -Bootloader        # reboot the 16U2 into HoodLoader2 for re-flashing
#>
param(
  [string]$Port,
  [switch]$Move,
  [string]$Type,
  [switch]$Bootloader
)
$ErrorActionPreference = "Stop"
$SYNC = 0xA5
function Frame([byte]$t, [byte[]]$payload = @()) {
  $body = @($t, [byte]$payload.Length) + $payload
  $crc = 0; foreach ($b in $body) { $crc = $crc -bxor $b }
  return [byte[]](@($SYNC) + $body + @([byte]$crc))
}
if (-not $Port) {
  $dev = Get-PnpDevice -PresentOnly -Class Ports | Where-Object { $_.InstanceId -match "VID_1A86" } | Select-Object -First 1
  if (-not $dev) { throw "No CH340 serial port found. Plug the USB-TTL adapter into this PC, or pass -Port COMx." }
  $Port = $dev.FriendlyName -replace '.*\((COM\d+)\).*', '$1'
}
$p = New-Object System.IO.Ports.SerialPort $Port, 38400
$p.DtrEnable = $false; $p.RtsEnable = $false; $p.ReadTimeout = 300
$p.Open()
Write-Host "CH340 on $Port @ 38400"
function Send([byte[]]$bytes) { $p.Write($bytes, 0, $bytes.Length) }
function ReadAll { $buf = New-Object byte[] 256; $n = 0; try { $n = $p.Read($buf, 0, $buf.Length) } catch [System.TimeoutException] {}; if ($n -gt 0) { $buf[0..($n-1)] } else { @() } }

# ---- ping
$p.DiscardInBuffer()
$ok = $false
for ($i = 0; $i -lt 3 -and -not $ok; $i++) {
  Send (Frame 0x20)
  Start-Sleep -Milliseconds 250
  $r = @(ReadAll)
  for ($j = 0; $j -le $r.Length - 5; $j++) {
    if ($r[$j] -eq 0xA5 -and $r[$j+1] -eq 0xA0 -and $r[$j+2] -eq 1) {
      $leds = $r[$j+3]; $ok = $true
      $l = @(); if ($leds -band 1) { $l += "NUM" }; if ($leds -band 2) { $l += "CAPS" }; if ($leds -band 4) { $l += "SCRL" }
      Write-Host ("PING OK - 16U2 answered. Target keyboard LEDs: " + ($(if ($l) { $l -join " " } else { "none" })))
      break
    }
  }
}
if (-not $ok) {
  Write-Host "NO REPLY from the 16U2." -ForegroundColor Red
  Write-Host "  Check: CH340 TXD -> D2, RXD -> D3, GND -> GND; Uno's D13 LED should flicker when this runs."
  Write-Host "  D13 flickers but no reply = RXD/D3 side; nothing flickers = TXD/D2 side or GND."
  $p.Close(); exit 1
}

if ($Bootloader) {
  Send (Frame 0x7E ([byte[]](0x42, 0x4C)))
  Write-Host "Bootloader command sent - the target should now show a 'HoodLoader2 Uno' COM port."
  $p.Close(); exit 0
}

if ($Move) {
  Write-Host "Moving the target's mouse in a square..."
  $steps = @(@(20,0), @(0,20), @(-20,0), @(0,-20))
  foreach ($rep in 1..3) { foreach ($s in $steps) { foreach ($k in 1..4) { Send (Frame 0x10 ([byte[]](($s[0] -band 0xFF), ($s[1] -band 0xFF), 0))); Start-Sleep -Milliseconds 15 } } }
}

if ($Type) {
  # HID usage codes (page 7): a-z = 4..29, 1-9 = 30..38, 0 = 39, space = 44
  Write-Host "Typing '$Type' on the target in 5 s - click into a text box on the target now..."
  Start-Sleep -Seconds 5
  foreach ($ch in $Type.ToLower().ToCharArray()) {
    $code = $null
    if ($ch -ge 'a' -and $ch -le 'z') { $code = 4 + ([int][char]$ch - [int][char]'a') }
    elseif ($ch -ge '1' -and $ch -le '9') { $code = 30 + ([int][char]$ch - [int][char]'1') }
    elseif ($ch -eq '0') { $code = 39 }
    elseif ($ch -eq ' ') { $code = 44 }
    if ($null -eq $code) { continue }
    Send (Frame 0x01 ([byte[]]@([byte]$code))); Start-Sleep -Milliseconds 30
    Send (Frame 0x02 ([byte[]]@([byte]$code))); Start-Sleep -Milliseconds 40
  }
  Write-Host "typed."
}
Send (Frame 0x1F)
$p.Close()
