// Copy to include/secrets.h (git-ignored) and fill in.
// These are compile-time DEFAULTS. Values saved later over serial or BLE (stored in NVS flash)
// take priority, so you can re-provision a device without re-flashing.
#pragma once

#define WIFI_SSID      "your-wifi-name"
#define WIFI_PASSWORD  "your-wifi-password"

// The laptop running the backend, as seen from the ESP32: its LAN IP, NOT "localhost".
// macOS: System Settings → Wi-Fi → Details → IP address   (or: ipconfig getifaddr en0)
#define BACKEND_HOST   "192.168.1.50"
#define BACKEND_PORT   8000

// Must match DEVICE_TOKEN in the backend .env (leave "" if unused).
#define DEVICE_TOKEN   ""
