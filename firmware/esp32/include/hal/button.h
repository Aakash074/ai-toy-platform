// Debounced push button producing PRESS / RELEASE events (polling; no ISR needed).
#pragma once
#include <Arduino.h>

class Button {
 public:
  enum class Event : uint8_t { NONE, PRESS, RELEASE };

  Button(int pin, bool activeLow, uint16_t debounceMs) : pin_(pin), activeLow_(activeLow), debounceMs_(debounceMs) {}
  void begin();
  Event poll();                 // call every loop()
  bool isDown() const { return stable_; }
  uint32_t heldMs() const { return stable_ ? millis() - pressedAt_ : 0; }
  bool rawDown() const;

 private:
  int pin_;
  bool activeLow_;
  uint16_t debounceMs_;
  bool stable_ = false;
  bool lastRaw_ = false;
  uint32_t lastChange_ = 0;
  uint32_t pressedAt_ = 0;
};
