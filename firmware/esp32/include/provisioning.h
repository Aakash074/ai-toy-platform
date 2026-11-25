// Network configuration storage + provisioning.
//
// Priority: values saved in NVS (via serial console or BLE)  >  compile-time secrets.h defaults.
//
// Serial console commands (115200 baud, end with newline):
//   ssid <name>          set Wi-Fi SSID          pass <password>   set Wi-Fi password
//   server <host> [port] set backend host/port   token <token>     set device token
//   save                 store & reboot          forget            erase saved config & reboot
//   status               print config/state      ble               reboot into BLE provisioning
//   reboot
#pragma once
#include <Arduino.h>

struct NetConfig {
  String ssid;
  String pass;
  String host;
  uint16_t port = 8000;
  String token;
  bool valid() const { return ssid.length() > 0 && host.length() > 0; }
};

void loadNetConfig(NetConfig& cfg);
void saveNetConfig(const NetConfig& cfg);
void forgetNetConfig();
void requestBleModeOnNextBoot();
bool consumeBleModeRequest();

class StatusLed;
// Blocking: advertises a BLE GATT service, waits for credentials, saves them, reboots. Wi-Fi is off.
void runBleProvisioning(const String& deviceId, StatusLed& led);

// Non-blocking serial command handler; returns true if config changed & reboot is pending.
void handleSerialConsole(NetConfig& cfg, const String& deviceId, const char* stateName);
