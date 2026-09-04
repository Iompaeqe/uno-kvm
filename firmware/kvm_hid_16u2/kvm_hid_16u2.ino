// Uno KVM — HID firmware for the Uno R3's ATmega16U2 (the USB chip).
//
// Runs under HoodLoader2 (board "HoodLoader2 16u2", library HID-Project).
// The 16U2 enumerates on the TARGET machine as a boot-protocol USB keyboard +
// mouse (works in BIOS/UEFI/GRUB) and receives commands over its hardware
// UART (Serial1), which on the Uno is wired to the ATmega328P. The 328P runs
// kvm_bridge_328p and just relays bytes from the CH340 adapter (the laptop).
//
// Wire protocol (laptop -> HID), one frame per command:
//   0xA5  type  len  payload[len]  xor(type, len, payload...)
//
//   type 0x01  KEY_PRESS       payload: usage code (HID usage page 7; 0xE0-0xE7 = modifiers)
//   type 0x02  KEY_RELEASE     payload: usage code
//   type 0x03  KEY_RELEASE_ALL no payload
//   type 0x10  MOUSE_MOVE      payload: dx, dy, wheel (int8 each)
//   type 0x11  MOUSE_BUTTONS   payload: mask (bit0 left, bit1 right, bit2 middle) — absolute state
//   type 0x1F  RELEASE_ALL     no payload: keyboard + mouse buttons
//   type 0x20  PING            no payload; reply 0xA5 0xA0 0x01 <leds> xor  (leds: bit0 num, bit1 caps, bit2 scroll)
//   type 0x7E  BOOTLOADER      payload: 0x42 0x4C ("BL"); reboots the 16U2 into HoodLoader2 so it can be
//                              re-flashed over USB. Needed because this build has NO USB serial interface:
//                              the 16U2 has only 4 endpoints and CDC would eat 3, leaving room for a single
//                              HID interface (build with -DCDC_DISABLED, see flash.ps1).
//
// Safety: if no valid frame arrives for LINK_TIMEOUT_MS while anything is
// held, everything is released (a dead link must never leave a key stuck).
//
// Boot banner "KVM-HID 1" is sent on Serial1 at start.

#include <HID-Project.h>
#include <avr/wdt.h>

#define LINK          Serial1
#define LINK_BAUD     38400
#define LINK_TIMEOUT_MS 2500
#define FW_VERSION    1

// 16U2 board LEDs (HoodLoader2 pinout): 17 = RX LED, 18 = TX LED, inverted logic.
#define LED_RX 17
#define LED_TX 18

enum : uint8_t {
  SYNC             = 0xA5,
  T_KEY_PRESS      = 0x01,
  T_KEY_RELEASE    = 0x02,
  T_KEY_RELEASE_ALL= 0x03,
  T_MOUSE_MOVE     = 0x10,
  T_MOUSE_BUTTONS  = 0x11,
  T_RELEASE_ALL    = 0x1F,
  T_PING           = 0x20,
  T_BOOTLOADER     = 0x7E,
  T_PONG           = 0xA0,
};

// Same trick the Arduino core uses for the 1200-baud touch: leave the magic key
// in RAM and let the watchdog reset us; HoodLoader2 sees the key and stays in
// bootloader mode (USB-serial, "HoodLoader2 Uno") instead of running this sketch.
#ifndef MAGIC_KEY
#define MAGIC_KEY 0x7777
#endif
static void releaseEverything();
static void jumpToBootloader() {
  releaseEverything();
  delay(20);
  cli();
  *(volatile uint16_t *)MAGIC_KEY_POS = MAGIC_KEY;
  wdt_enable(WDTO_120MS);
  for (;;) {}
}

enum State : uint8_t { S_SYNC, S_TYPE, S_LEN, S_PAYLOAD, S_CRC };

static State   st = S_SYNC;
static uint8_t fType, fLen, fPos, fCrc;
static uint8_t fBuf[8];
static uint8_t mouseMask = 0;
static uint8_t keysHeld = 0;         // count of pressed keys we know about
static unsigned long lastFrame = 0;
static unsigned long ledOffAt = 0;

static void releaseEverything() {
  BootKeyboard.releaseAll();
  BootMouse.releaseAll();
  mouseMask = 0;
  keysHeld = 0;
}

static void sendPong() {
  uint8_t leds = BootKeyboard.getLeds();
  uint8_t crc = T_PONG ^ 1 ^ leds;
  LINK.write(SYNC); LINK.write(T_PONG); LINK.write((uint8_t)1); LINK.write(leds); LINK.write(crc);
}

static void applyMouseButtons(uint8_t mask) {
  mask &= 0x07;
  uint8_t changed = mask ^ mouseMask;
  // HID-Project: MOUSE_LEFT=1, MOUSE_RIGHT=2, MOUSE_MIDDLE=4 — same bit layout as ours.
  for (uint8_t bit = 1; bit <= 4; bit <<= 1) {
    if (!(changed & bit)) continue;
    if (mask & bit) BootMouse.press(bit); else BootMouse.release(bit);
  }
  mouseMask = mask;
}

static void handleFrame() {
  switch (fType) {
    case T_KEY_PRESS:
      if (fLen == 1 && fBuf[0] >= 4 && fBuf[0] <= 0xE7) {
        BootKeyboard.press((KeyboardKeycode)fBuf[0]);
        if (keysHeld < 255) keysHeld++;
      }
      break;
    case T_KEY_RELEASE:
      if (fLen == 1) {
        BootKeyboard.release((KeyboardKeycode)fBuf[0]);
        if (keysHeld) keysHeld--;
      }
      break;
    case T_KEY_RELEASE_ALL:
      BootKeyboard.releaseAll();
      keysHeld = 0;
      break;
    case T_MOUSE_MOVE:
      if (fLen == 3) BootMouse.move((int8_t)fBuf[0], (int8_t)fBuf[1], (int8_t)fBuf[2]);
      break;
    case T_MOUSE_BUTTONS:
      if (fLen == 1) applyMouseButtons(fBuf[0]);
      break;
    case T_RELEASE_ALL:
      releaseEverything();
      break;
    case T_PING:
      sendPong();
      break;
    case T_BOOTLOADER:
      if (fLen == 2 && fBuf[0] == 0x42 && fBuf[1] == 0x4C) jumpToBootloader();
      break;
    default:
      break;
  }
}

static void feed(uint8_t b) {
  switch (st) {
    case S_SYNC:
      if (b == SYNC) st = S_TYPE;
      break;
    case S_TYPE:
      fType = b; fCrc = b; st = S_LEN;
      break;
    case S_LEN:
      if (b > sizeof(fBuf)) { st = S_SYNC; break; }
      fLen = b; fCrc ^= b; fPos = 0;
      st = fLen ? S_PAYLOAD : S_CRC;
      break;
    case S_PAYLOAD:
      fBuf[fPos++] = b; fCrc ^= b;
      if (fPos >= fLen) st = S_CRC;
      break;
    case S_CRC:
      st = S_SYNC;
      if (b == fCrc) {
        lastFrame = millis();
        digitalWrite(LED_RX, LOW); ledOffAt = lastFrame + 30;
        handleFrame();
      }
      break;
  }
}

void setup() {
  pinMode(LED_RX, OUTPUT); pinMode(LED_TX, OUTPUT);
  digitalWrite(LED_RX, HIGH); digitalWrite(LED_TX, HIGH);
  LINK.begin(LINK_BAUD);
  BootKeyboard.begin();
  BootMouse.begin();
  LINK.print(F("KVM-HID ")); LINK.println(FW_VERSION);
  lastFrame = millis();
}

void loop() {
  while (LINK.available()) feed((uint8_t)LINK.read());

  unsigned long now = millis();
  if (ledOffAt && (long)(now - ledOffAt) >= 0) { digitalWrite(LED_RX, HIGH); ledOffAt = 0; }

  if ((keysHeld || mouseMask) && (now - lastFrame) > LINK_TIMEOUT_MS) {
    releaseEverything();
    lastFrame = now;
  }
}
