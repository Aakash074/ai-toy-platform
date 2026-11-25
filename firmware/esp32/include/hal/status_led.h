// Non-blocking single-LED status indicator. Each state has a distinct blink pattern:
//   BOOTING          solid on
//   CONNECTING_WIFI  fast blink (5 Hz)
//   CONNECTED        slow "breathing" blip every 2 s (idle, ready)
//   LISTENING        solid on
//   THINKING         medium blink (2.5 Hz)
//   SPEAKING         double-blip
//   ERROR            SOS-like triple flash, then pause
//   PROVISIONING     long-on / short-off (BLE setup)
// Swap this class for an RGB/NeoPixel implementation later without touching main.cpp.
#pragma once
#include <Arduino.h>
#include "toy_state.h"

class StatusLed {
 public:
  StatusLed(int pin, bool activeHigh) : pin_(pin), activeHigh_(activeHigh) {}
  void begin();
  void setState(ToyState s);
  void identify(uint32_t ms = 3000);  // rapid flashing so you can find the device
  void update();                      // call every loop()

 private:
  void write(bool on);
  int pin_;
  bool activeHigh_;
  ToyState state_ = ToyState::BOOTING;
  uint32_t phaseStart_ = 0;
  uint32_t identifyUntil_ = 0;
};
