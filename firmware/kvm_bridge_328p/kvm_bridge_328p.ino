// Uno KVM — byte bridge for the Uno R3's ATmega328P.
//
// The laptop's CH340 USB-TTL adapter is wired to D2/D3 (SoftwareSerial); the
// 328P's hardware UART (D0/D1) is wired on the board to the ATmega16U2, which
// runs kvm_hid_16u2. This sketch only relays bytes in both directions.
// Upload with board "HoodLoader2 Uno" once HoodLoader2 is on the 16U2.
//
//   CH340 TXD -> D2 (RX)      CH340 RXD <- D3 (TX)      CH340 GND -- GND
//   (do NOT connect the CH340's VCC pin)
//
// D13 (built-in LED) flickers on traffic — a quick wiring check.

#include <SoftwareSerial.h>

#define BAUD 38400
#define PIN_RX 2   // from CH340 TXD
#define PIN_TX 3   // to   CH340 RXD

SoftwareSerial link(PIN_RX, PIN_TX);
static unsigned long ledOffAt = 0;

static inline void blip() { digitalWrite(LED_BUILTIN, HIGH); ledOffAt = millis() + 20; }

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(BAUD);   // to the 16U2
  link.begin(BAUD);     // to the laptop
}

void loop() {
  while (link.available()) { Serial.write((uint8_t)link.read()); blip(); }
  while (Serial.available()) { link.write((uint8_t)Serial.read()); blip(); }
  if (ledOffAt && (long)(millis() - ledOffAt) >= 0) { digitalWrite(LED_BUILTIN, LOW); ledOffAt = 0; }
}
