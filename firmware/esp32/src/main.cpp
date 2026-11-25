// AI Toy Lab — ESP32 toy controller (V1)
//
// The ESP32 is the toy's "body": identity, Wi-Fi, status LED, talk button, backend backend.
// All audio and AI happen on the laptop/backend in V1 — no LLM or audio processing runs here.
//
// State machine:  BOOTING → CONNECTING_WIFI → CONNECTED ⇄ LISTENING → THINKING → SPEAKING → CONNECTED
//                                      ↑______________ ERROR / disconnect ______________|
#include <Arduino.h>
#include <WiFi.h>

#include "backend_link.h"
#include "config.h"
#include "hal/audio_hal.h"
#include "hal/button.h"
#include "hal/status_led.h"
#include "ota.h"
#include "provisioning.h"
#include "toy_state.h"

static StatusLed led(LED_PIN, LED_ACTIVE_HIGH);
static Button button(BUTTON_PIN, BUTTON_ACTIVE_LOW, BUTTON_DEBOUNCE_MS);
static BackendLink backend;
static NullAudioInput micHal;      // Phase 4: replace with an I2S microphone implementation
static NullAudioOutput speakerHal; // Phase 4: replace with an I2S amplifier implementation
static NetConfig net;
static String deviceId;

static ToyState state = ToyState::BOOTING;
static uint32_t stateSince = 0;
static bool wifiUp = false;
static bool linkStarted = false;
static bool otaChecked = false;
static uint32_t wifiAttemptAt = 0;
static uint8_t wifiAttempts = 0;

static void setState(ToyState s, bool report = false) {
  if (s == state) return;
  Serial.printf("[state] %s -> %s\n", toyStateName(state), toyStateName(s));
  state = s;
  stateSince = millis();
  led.setState(s);
  if (report) backend.sendState(toyStateName(s));
}

static String makeDeviceId() {
  uint64_t mac = ESP.getEfuseMac();  // factory-burned, unique per chip
  char buf[20];
  snprintf(buf, sizeof(buf), "esp32-%02x%02x%02x", (uint8_t)(mac >> 24), (uint8_t)(mac >> 32), (uint8_t)(mac >> 40));
  return String(buf);
}

static void startWifi() {
  Serial.printf("[wifi] connecting to '%s' (attempt %u)\n", net.ssid.c_str(), wifiAttempts + 1);
  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(deviceId.c_str());
  WiFi.setAutoReconnect(true);
  WiFi.begin(net.ssid.c_str(), net.pass.c_str());
  wifiAttemptAt = millis();
  wifiAttempts++;
}

static void onBackendMessage(JsonDocument& msg) {
  const char* t = msg["type"] | "";
  if (strcmp(t, "welcome") == 0) {
    Serial.printf("[ws] registered as %s, personality: %s\n", deviceId.c_str(), (const char*)(msg["personality"]["name"] | "?"));
    setState(ToyState::CONNECTED);
    if (!otaChecked) { otaChecked = true; otaCheck(net, deviceId); }
  } else if (strcmp(t, "set_state") == 0) {
    ToyState s;
    if (toyStateFromName(msg["state"] | "", s)) setState(s);
  } else if (strcmp(t, "identify") == 0) {
    Serial.println("[cmd] identify");
    led.identify();
  } else if (strcmp(t, "personality") == 0) {
    Serial.printf("[cmd] personality -> %s\n", (const char*)(msg["name"] | "?"));
    led.identify(600);
  } else if (strcmp(t, "ota_check") == 0) {
    otaCheck(net, deviceId);
  } else if (strcmp(t, "reboot") == 0) {
    Serial.println("[cmd] reboot requested by backend");
    delay(200);
    ESP.restart();
  } else if (strcmp(t, "error") == 0) {
    Serial.printf("[ws] backend error: %s\n", (const char*)(msg["message"] | "?"));
    setState(ToyState::ERROR);
  } else if (strcmp(t, "_disconnected") == 0) {
    if (wifiUp) setState(ToyState::CONNECTING_WIFI);  // Wi-Fi fine, backend unreachable: keep retrying
  }
}

static bool bootHoldRequestsBle() {
  // Press & hold the button within 1.5 s after reset, for BOOT_HOLD_FOR_BLE_MS, to enter BLE setup.
  // (Pressing GPIO0 *during* reset would enter the ROM flasher instead, so we wait for a fresh press.)
  if (BUTTON_PIN < 0) return false;
  Serial.println("[boot] hold the button now for BLE provisioning...");
  uint32_t start = millis();
  while (millis() - start < 1500) {
    led.update();
    if (button.rawDown()) {
      uint32_t held = millis();
      while (button.rawDown()) {
        led.update();
        if (millis() - held >= BOOT_HOLD_FOR_BLE_MS) return true;
        delay(10);
      }
    }
    delay(10);
  }
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  deviceId = makeDeviceId();
  led.begin();
  led.setState(ToyState::BOOTING);
  button.begin();
  micHal.begin(AudioFormat{});
  speakerHal.begin(AudioFormat{});

  Serial.printf("\n=== AI Toy Lab firmware %s ===\n", FIRMWARE_VERSION);
  Serial.printf("[boot] device_id=%s chip=%s rev%d heap=%u LED_PIN=%d BUTTON_PIN=%d\n", deviceId.c_str(),
                ESP.getChipModel(), ESP.getChipRevision(), ESP.getFreeHeap(), LED_PIN, BUTTON_PIN);
  Serial.printf("[boot] audio hardware: mic=%s speaker=%s (V1 uses the laptop)\n",
                micHal.available() ? "yes" : "none", speakerHal.available() ? "yes" : "none");

  loadNetConfig(net);
  bool wantBle = consumeBleModeRequest() || bootHoldRequestsBle();
  if (!net.valid()) {
    Serial.println("[boot] no Wi-Fi/backend config. Use serial commands (type 'help') or BLE provisioning.");
#if ENABLE_BLE_PROVISIONING
    wantBle = true;
#endif
  }
  if (wantBle) runBleProvisioning(deviceId, led);  // does not return (reboots after commit)

  if (!net.valid()) { setState(ToyState::ERROR); return; }
  setState(ToyState::CONNECTING_WIFI);
  startWifi();
}

void loop() {
  led.update();
  handleSerialConsole(net, deviceId, toyStateName(state));
  if (!net.valid()) { delay(10); return; }

  // ---------- Wi-Fi supervision (connect, detect loss, retry with backoff) ----------
  const bool up = WiFi.status() == WL_CONNECTED;
  if (up && !wifiUp) {
    wifiUp = true;
    wifiAttempts = 0;
    Serial.printf("[wifi] connected ip=%s rssi=%d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
    if (!linkStarted) {
      backend.begin(net, deviceId, onBackendMessage, [] { return toyStateName(state); });
      linkStarted = true;
    }
  } else if (!up && wifiUp) {
    wifiUp = false;
    otaChecked = false;
    Serial.println("[wifi] connection lost");
    setState(ToyState::CONNECTING_WIFI);
    wifiAttemptAt = millis();
  } else if (!up) {
    const uint32_t backoff = min<uint32_t>(WIFI_CONNECT_TIMEOUT_MS * (1u << min<uint8_t>(wifiAttempts, 3)), 120000);
    if (millis() - wifiAttemptAt > backoff) {
      Serial.printf("[wifi] still not connected (status=%d), retrying\n", WiFi.status());
      if (wifiAttempts >= 3) setState(ToyState::ERROR);  // visible on the LED, keeps retrying
      startWifi();
    }
  }

  if (wifiUp && linkStarted) backend.loop();

  // ---------- talk button ----------
  switch (button.poll()) {
    case Button::Event::PRESS:
      Serial.println("[button] press");
      if (backend.online()) {
        backend.sendButton("press");
        setState(ToyState::LISTENING);  // optimistic; the backend confirms with set_state
      } else {
        Serial.println("[button] ignored: not connected to backend");
        led.identify(300);
      }
      break;
    case Button::Event::RELEASE:
      Serial.println("[button] release");
      if (backend.online()) backend.sendButton("release");
      break;
    default:
      break;
  }

  // ---------- recover from a lost backend update ----------
  if ((state == ToyState::LISTENING || state == ToyState::THINKING || state == ToyState::SPEAKING) &&
      millis() - stateSince > STATE_STUCK_TIMEOUT_MS) {
    Serial.println("[state] no update from backend, returning to CONNECTED");
    setState(backend.online() ? ToyState::CONNECTED : ToyState::CONNECTING_WIFI, true);
  }
  if (state == ToyState::ERROR && backend.online() && millis() - stateSince > 3000) setState(ToyState::CONNECTED);

  delay(2);
}
