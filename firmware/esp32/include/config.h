// Board / behaviour configuration. Every value can be overridden from platformio.ini build_flags,
// e.g.  -DLED_PIN=4  -DLED_ACTIVE_HIGH=0  -DBUTTON_PIN=15
#pragma once

#ifndef FIRMWARE_VERSION
#define FIRMWARE_VERSION "0.1.0-dev"
#endif

// ---------- Status LED ----------
// -1 disables the LED driver (states are still printed on the serial console).
#ifndef LED_PIN
#define LED_PIN 2
#endif
#ifndef LED_ACTIVE_HIGH
#define LED_ACTIVE_HIGH 1
#endif

// ---------- Talk button ----------
// Default GPIO0 = the BOOT button present on almost every ESP32 dev board (active LOW).
// GPIO0 is a strapping pin: don't hold it while pressing EN/reset or the chip enters flash mode.
#ifndef BUTTON_PIN
#define BUTTON_PIN 0
#endif
#ifndef BUTTON_ACTIVE_LOW
#define BUTTON_ACTIVE_LOW 1
#endif
#ifndef BUTTON_DEBOUNCE_MS
#define BUTTON_DEBOUNCE_MS 30
#endif
// Hold the button this long during boot to enter BLE provisioning mode.
#ifndef BOOT_HOLD_FOR_BLE_MS
#define BOOT_HOLD_FOR_BLE_MS 3000
#endif

// ---------- Networking ----------
#ifndef WIFI_CONNECT_TIMEOUT_MS
#define WIFI_CONNECT_TIMEOUT_MS 20000
#endif
#ifndef WS_RECONNECT_MS
#define WS_RECONNECT_MS 3000
#endif
#ifndef DEFAULT_HEARTBEAT_S
#define DEFAULT_HEARTBEAT_S 10
#endif
// If the backend leaves us in LISTENING/THINKING/SPEAKING this long without an update, fall back.
#ifndef STATE_STUCK_TIMEOUT_MS
#define STATE_STUCK_TIMEOUT_MS 60000
#endif

// ---------- BLE provisioning ----------
// BLE is only started in provisioning mode, with Wi-Fi OFF, so the two radios never run
// together (keeps RAM use low on classic ESP32 and avoids coexistence issues).
#ifndef ENABLE_BLE_PROVISIONING
#define ENABLE_BLE_PROVISIONING 1
#endif
#define BLE_SERVICE_UUID   "6b1a0001-4d5e-4a8f-9a3c-a1700e000001"
#define BLE_CHAR_SSID      "6b1a0002-4d5e-4a8f-9a3c-a1700e000001"
#define BLE_CHAR_PASS      "6b1a0003-4d5e-4a8f-9a3c-a1700e000001"
#define BLE_CHAR_HOST      "6b1a0004-4d5e-4a8f-9a3c-a1700e000001"
#define BLE_CHAR_PORT      "6b1a0005-4d5e-4a8f-9a3c-a1700e000001"
#define BLE_CHAR_COMMIT    "6b1a0006-4d5e-4a8f-9a3c-a1700e000001"
#define BLE_CHAR_INFO      "6b1a0007-4d5e-4a8f-9a3c-a1700e000001"

// ---------- Future audio hardware (Phase 4, unused in V1) ----------
// I2S microphone (e.g. INMP441) and I2S amplifier (e.g. MAX98357A). -1 = not fitted.
#ifndef I2S_MIC_SCK
#define I2S_MIC_SCK -1
#define I2S_MIC_WS  -1
#define I2S_MIC_SD  -1
#endif
#ifndef I2S_SPK_BCLK
#define I2S_SPK_BCLK -1
#define I2S_SPK_LRC  -1
#define I2S_SPK_DIN  -1
#endif
