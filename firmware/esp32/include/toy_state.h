// Device state machine — names must match backend/devices/registry.py STATES.
#pragma once
#include <Arduino.h>

enum class ToyState : uint8_t {
  BOOTING,
  CONNECTING_WIFI,
  CONNECTED,
  LISTENING,
  THINKING,
  SPEAKING,
  ERROR,
  PROVISIONING,  // internal (BLE setup mode); reported as CONNECTING_WIFI
};

inline const char* toyStateName(ToyState s) {
  switch (s) {
    case ToyState::BOOTING: return "BOOTING";
    case ToyState::CONNECTING_WIFI: return "CONNECTING_WIFI";
    case ToyState::CONNECTED: return "CONNECTED";
    case ToyState::LISTENING: return "LISTENING";
    case ToyState::THINKING: return "THINKING";
    case ToyState::SPEAKING: return "SPEAKING";
    case ToyState::ERROR: return "ERROR";
    case ToyState::PROVISIONING: return "CONNECTING_WIFI";
  }
  return "ERROR";
}

inline bool toyStateFromName(const char* n, ToyState& out) {
  static const ToyState all[] = {ToyState::BOOTING, ToyState::CONNECTING_WIFI, ToyState::CONNECTED, ToyState::LISTENING,
                                 ToyState::THINKING, ToyState::SPEAKING, ToyState::ERROR};
  for (ToyState s : all) {
    if (strcmp(n, toyStateName(s)) == 0) { out = s; return true; }
  }
  return false;
}
