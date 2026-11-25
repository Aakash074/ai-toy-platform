// WebSocket link to the AI Toy Lab backend (ws://host:port/ws/device/<id>).
// Handles hello/welcome handshake, heartbeats, automatic reconnection and JSON command dispatch.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <WebSocketsClient.h>

#include <functional>

#include "provisioning.h"

class BackendLink {
 public:
  using CommandHandler = std::function<void(JsonDocument&)>;
  using StateProvider = std::function<const char*()>;

  void begin(const NetConfig& cfg, const String& deviceId, CommandHandler onCommand, StateProvider state);
  void loop();
  bool online() const { return registered_; }

  void sendButton(const char* action);
  void sendState(const char* state);
  void sendLog(const char* level, const String& message);

 private:
  void onEvent(WStype_t type, uint8_t* payload, size_t length);
  void sendHello();
  void sendHeartbeat();
  void sendJson(JsonDocument& doc);

  WebSocketsClient ws_;
  String deviceId_;
  CommandHandler onCommand_;
  StateProvider state_;
  bool registered_ = false;
  uint32_t heartbeatMs_ = 10000;
  uint32_t lastHeartbeat_ = 0;
};
