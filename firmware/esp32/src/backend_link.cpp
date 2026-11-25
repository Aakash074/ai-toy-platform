#include "backend_link.h"

#include <WiFi.h>

#include "config.h"

void BackendLink::begin(const NetConfig& cfg, const String& deviceId, CommandHandler onCommand, StateProvider state) {
  deviceId_ = deviceId;
  onCommand_ = onCommand;
  state_ = state;
  heartbeatMs_ = DEFAULT_HEARTBEAT_S * 1000UL;
  String path = "/ws/device/" + deviceId;
  if (cfg.token.length()) path += "?token=" + cfg.token;
  ws_.begin(cfg.host.c_str(), cfg.port, path.c_str());
  ws_.onEvent([this](WStype_t t, uint8_t* p, size_t l) { onEvent(t, p, l); });
  ws_.setReconnectInterval(WS_RECONNECT_MS);
  ws_.enableHeartbeat(15000, 3000, 2);  // WS-level ping/pong: detects dead TCP links quickly
  Serial.printf("[ws] connecting to ws://%s:%u%s\n", cfg.host.c_str(), cfg.port, ("/ws/device/" + deviceId).c_str());
}

void BackendLink::loop() {
  ws_.loop();
  if (registered_ && millis() - lastHeartbeat_ >= heartbeatMs_) sendHeartbeat();
}

void BackendLink::onEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      Serial.println("[ws] connected, sending hello");
      sendHello();
      break;
    case WStype_DISCONNECTED:
      if (registered_) Serial.println("[ws] disconnected from backend (auto-reconnecting)");
      registered_ = false;
      {
        JsonDocument doc;
        doc["type"] = "_disconnected";
        if (onCommand_) onCommand_(doc);
      }
      break;
    case WStype_TEXT: {
      JsonDocument doc;
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) { Serial.printf("[ws] bad JSON: %s\n", err.c_str()); return; }
      const char* t = doc["type"] | "";
      if (strcmp(t, "welcome") == 0) {
        registered_ = true;
        heartbeatMs_ = (doc["heartbeat_interval_s"] | DEFAULT_HEARTBEAT_S) * 1000UL;
        sendHeartbeat();
      } else if (strcmp(t, "ping") == 0) {
        JsonDocument pong;
        pong["type"] = "pong";
        sendJson(pong);
      }
      if (onCommand_) onCommand_(doc);
      break;
    }
    case WStype_ERROR:
      Serial.println("[ws] error");
      break;
    default:
      break;
  }
}

void BackendLink::sendJson(JsonDocument& doc) {
  String out;
  serializeJson(doc, out);
  ws_.sendTXT(out);
}

void BackendLink::sendHello() {
  JsonDocument doc;
  doc["type"] = "hello";
  doc["name"] = "ESP32 Toy " + deviceId_.substring(deviceId_.length() - 6);
  doc["firmware"] = FIRMWARE_VERSION;
#if defined(CONFIG_IDF_TARGET_ESP32S3)
  doc["hardware"] = "esp32-s3";
#elif defined(CONFIG_IDF_TARGET_ESP32C3)
  doc["hardware"] = "esp32-c3";
#else
  doc["hardware"] = "esp32";
#endif
  doc["mac"] = WiFi.macAddress();
  doc["ip"] = WiFi.localIP().toString();
  JsonObject caps = doc["capabilities"].to<JsonObject>();
  caps["led"] = LED_PIN >= 0;
  caps["button"] = BUTTON_PIN >= 0;
  caps["ble"] = (bool)ENABLE_BLE_PROVISIONING;
  caps["audio"] = false;  // V1: laptop handles audio
  sendJson(doc);
}

void BackendLink::sendHeartbeat() {
  lastHeartbeat_ = millis();
  JsonDocument doc;
  doc["type"] = "heartbeat";
  doc["rssi"] = WiFi.RSSI();
  doc["uptime_s"] = millis() / 1000;
  doc["free_heap"] = ESP.getFreeHeap();
  doc["state"] = state_ ? state_() : "CONNECTED";
  sendJson(doc);
}

void BackendLink::sendButton(const char* action) {
  if (!registered_) return;
  JsonDocument doc;
  doc["type"] = "button";
  doc["action"] = action;
  sendJson(doc);
}

void BackendLink::sendState(const char* state) {
  if (!registered_) return;
  JsonDocument doc;
  doc["type"] = "state";
  doc["state"] = state;
  sendJson(doc);
}

void BackendLink::sendLog(const char* level, const String& message) {
  if (!registered_) return;
  JsonDocument doc;
  doc["type"] = "log";
  doc["level"] = level;
  doc["message"] = message;
  sendJson(doc);
}
