#include "hal/status_led.h"

void StatusLed::begin() {
  if (pin_ < 0) return;
  pinMode(pin_, OUTPUT);
  write(false);
}

void StatusLed::write(bool on) {
  if (pin_ < 0) return;
  digitalWrite(pin_, (on == activeHigh_) ? HIGH : LOW);
}

void StatusLed::setState(ToyState s) {
  if (s == state_) return;
  state_ = s;
  phaseStart_ = millis();
}

void StatusLed::identify(uint32_t ms) { identifyUntil_ = millis() + ms; }

void StatusLed::update() {
  if (pin_ < 0) return;
  const uint32_t now = millis();
  if ((int32_t)(identifyUntil_ - now) > 0) { write((now / 60) % 2); return; }
  const uint32_t t = now - phaseStart_;
  bool on = false;
  switch (state_) {
    case ToyState::BOOTING:
    case ToyState::LISTENING:       on = true; break;
    case ToyState::CONNECTING_WIFI: on = (t % 200) < 100; break;
    case ToyState::CONNECTED:       on = (t % 2000) < 80; break;
    case ToyState::THINKING:        on = (t % 400) < 200; break;
    case ToyState::SPEAKING:        { uint32_t p = t % 700; on = p < 90 || (p > 180 && p < 270); } break;
    case ToyState::ERROR:           { uint32_t p = t % 1500; on = (p < 600) && ((p % 200) < 100); } break;
    case ToyState::PROVISIONING:    on = (t % 1200) < 900; break;
  }
  write(on);
}
