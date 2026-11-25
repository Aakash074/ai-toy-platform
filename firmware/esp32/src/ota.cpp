// OTA update architecture — V1 placeholder.
//
// Flow (implemented up to the "apply" step):
//   1. device asks  GET /api/ota/<device_id>?version=<fw>   (on connect, and on "ota_check" command)
//   2. backend answers {update_available, latest, url, sha256}
//   3. [future] download url with HTTPUpdate / Update.h into the inactive OTA slot
//      (partition table min_spiffs.csv already has two app slots), verify sha256 + signature,
//      set boot partition, reboot; roll back automatically if the new image fails to check in.
#include "ota.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include "config.h"

void otaCheck(const NetConfig& cfg, const String& deviceId) {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  String url = "http://" + cfg.host + ":" + String(cfg.port) + "/api/ota/" + deviceId + "?version=" FIRMWARE_VERSION;
  http.setTimeout(5000);
  if (!http.begin(url)) return;
  int code = http.GET();
  if (code == 200) {
    JsonDocument doc;
    if (!deserializeJson(doc, http.getString())) {
      bool avail = doc["update_available"] | false;
      Serial.printf("[ota] current=%s latest=%s update_available=%s\n", FIRMWARE_VERSION,
                    (const char*)(doc["latest"] | "?"), avail ? "yes" : "no");
      if (avail) Serial.println("[ota] update download/apply is not implemented in V1 (placeholder)");
    }
  } else {
    Serial.printf("[ota] check failed: HTTP %d\n", code);
  }
  http.end();
}
