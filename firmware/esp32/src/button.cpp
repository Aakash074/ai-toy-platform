#include "hal/button.h"

void Button::begin() {
  if (pin_ < 0) return;
  pinMode(pin_, activeLow_ ? INPUT_PULLUP : INPUT_PULLDOWN);
  lastRaw_ = stable_ = rawDown();
  lastChange_ = millis();
}

bool Button::rawDown() const {
  if (pin_ < 0) return false;
  const int v = digitalRead(pin_);
  return activeLow_ ? v == LOW : v == HIGH;
}

Button::Event Button::poll() {
  if (pin_ < 0) return Event::NONE;
  const bool raw = rawDown();
  const uint32_t now = millis();
  if (raw != lastRaw_) { lastRaw_ = raw; lastChange_ = now; }
  if (raw != stable_ && (now - lastChange_) >= debounceMs_) {
    stable_ = raw;
    if (stable_) { pressedAt_ = now; return Event::PRESS; }
    return Event::RELEASE;
  }
  return Event::NONE;
}
