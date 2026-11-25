#include "provisioning.h"

#include <Preferences.h>
#include <WiFi.h>

#include "config.h"
#include "hal/status_led.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "include/secrets.h not found - using empty defaults (provision over serial or BLE)"
#define WIFI_SSID ""
#define WIFI_PASSWORD ""
#define BACKEND_HOST ""
#define BACKEND_PORT 8000
#define DEVICE_TOKEN ""
#endif

static const char* NS = "aitoy";

void loadNetConfig(NetConfig& cfg) {
  cfg.ssid = WIFI_SSID;
  cfg.pass = WIFI_PASSWORD;
  cfg.host = BACKEND_HOST;
  cfg.port = BACKEND_PORT;
  cfg.token = DEVICE_TOKEN;
  Preferences p;
  if (p.begin(NS, true)) {  // read-only
    if (p.isKey("ssid")) cfg.ssid = p.getString("ssid", cfg.ssid);
    if (p.isKey("pass")) cfg.pass = p.getString("pass", cfg.pass);
    if (p.isKey("host")) cfg.host = p.getString("host", cfg.host);
    if (p.isKey("port")) cfg.port = p.getUShort("port", cfg.port);
    if (p.isKey("token")) cfg.token = p.getString("token", cfg.token);
    p.end();
  }
}

void saveNetConfig(const NetConfig& cfg) {
  Preferences p;
  p.begin(NS, false);
  p.putString("ssid", cfg.ssid);
  p.putString("pass", cfg.pass);
  p.putString("host", cfg.host);
  p.putUShort("port", cfg.port);
  p.putString("token", cfg.token);
  p.end();
}

void forgetNetConfig() {
  Preferences p;
  p.begin(NS, false);
  p.clear();
  p.end();
}

void requestBleModeOnNextBoot() {
  Preferences p;
  p.begin(NS, false);
  p.putBool("ble_next", true);
  p.end();
}

bool consumeBleModeRequest() {
  Preferences p;
  p.begin(NS, false);
  bool v = p.getBool("ble_next", false);
  if (v) p.remove("ble_next");
  p.end();
  return v;
}

// ------------------------------------------------------------------ serial console
void handleSerialConsole(NetConfig& cfg, const String& deviceId, const char* stateName) {
  static String line;
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\r') continue;
    if (ch != '\n') { if (line.length() < 200) line += ch; continue; }
    String cmd = line;
    line = "";
    cmd.trim();
    if (cmd.isEmpty()) continue;
    int sp = cmd.indexOf(' ');
    String verb = sp < 0 ? cmd : cmd.substring(0, sp);
    String arg = sp < 0 ? "" : cmd.substring(sp + 1);
    verb.toLowerCase();

    if (verb == "ssid") { cfg.ssid = arg; Serial.printf("ssid = '%s' (type 'save' to store)\n", arg.c_str()); }
    else if (verb == "pass") { cfg.pass = arg; Serial.println("password set (type 'save' to store)"); }
    else if (verb == "token") { cfg.token = arg; Serial.println("token set (type 'save' to store)"); }
    else if (verb == "server") {
      int s2 = arg.indexOf(' ');
      cfg.host = s2 < 0 ? arg : arg.substring(0, s2);
      if (s2 > 0) cfg.port = (uint16_t)arg.substring(s2 + 1).toInt();
      Serial.printf("server = %s:%u (type 'save' to store)\n", cfg.host.c_str(), cfg.port);
    }
    else if (verb == "save") { saveNetConfig(cfg); Serial.println("saved. rebooting..."); delay(300); ESP.restart(); }
    else if (verb == "forget") { forgetNetConfig(); Serial.println("config erased. rebooting..."); delay(300); ESP.restart(); }
    else if (verb == "ble") { requestBleModeOnNextBoot(); Serial.println("rebooting into BLE provisioning..."); delay(300); ESP.restart(); }
    else if (verb == "reboot") { ESP.restart(); }
    else if (verb == "scan") {
      Serial.println("scanning Wi-Fi (2.4 GHz)...");
      WiFi.disconnect();  // a scan cannot run while a connect attempt is in progress
      delay(100);
      int n = WiFi.scanNetworks();
      for (int i = 0; i < n; i++)
        Serial.printf("  %2d) '%s'  ch=%d rssi=%d enc=%d\n", i + 1, WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i), (int)WiFi.encryptionType(i));
      Serial.printf("%d networks found\n", n);
      WiFi.scanDelete();
    }
    else if (verb == "status") {
      Serial.printf("device_id=%s fw=%s state=%s\n", deviceId.c_str(), FIRMWARE_VERSION, stateName);
      Serial.printf("wifi ssid='%s' status=%d ip=%s rssi=%d\n", cfg.ssid.c_str(), WiFi.status(),
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
      Serial.printf("backend=%s:%u token=%s heap=%u\n", cfg.host.c_str(), cfg.port, cfg.token.isEmpty() ? "(none)" : "(set)",
                    ESP.getFreeHeap());
    }
    else Serial.println("commands: ssid <x> | pass <x> | server <host> [port] | token <x> | save | forget | status | scan | ble | reboot");
  }
}

// ------------------------------------------------------------------ BLE provisioning
#if ENABLE_BLE_PROVISIONING
#include <NimBLEDevice.h>

static NetConfig* gBleCfg = nullptr;
static volatile bool gCommit = false;

class ProvCallbacks : public NimBLECharacteristicCallbacks {
 public:
  explicit ProvCallbacks(char field) : field_(field) {}
  void onWrite(NimBLECharacteristic* c) override {
    String v = String(c->getValue().c_str());
    v.trim();
    switch (field_) {
      case 's': gBleCfg->ssid = v; Serial.printf("[ble] ssid='%s'\n", v.c_str()); break;
      case 'p': gBleCfg->pass = v; Serial.println("[ble] password received"); break;
      case 'h': gBleCfg->host = v; Serial.printf("[ble] host=%s\n", v.c_str()); break;
      case 'o': gBleCfg->port = (uint16_t)v.toInt(); Serial.printf("[ble] port=%u\n", gBleCfg->port); break;
      case 'c': if (v == "1") gCommit = true; break;
    }
  }

 private:
  char field_;
};

void runBleProvisioning(const String& deviceId, StatusLed& led) {
  static NetConfig cfg;
  loadNetConfig(cfg);
  gBleCfg = &cfg;
  WiFi.mode(WIFI_OFF);  // never run both radios at once on constrained boards
  led.setState(ToyState::PROVISIONING);

  String name = "AIToy-" + deviceId.substring(deviceId.length() - 6);
  NimBLEDevice::init(name.c_str());
  NimBLEServer* server = NimBLEDevice::createServer();
  NimBLEService* svc = server->createService(BLE_SERVICE_UUID);
  svc->createCharacteristic(BLE_CHAR_SSID, NIMBLE_PROPERTY::WRITE)->setCallbacks(new ProvCallbacks('s'));
  svc->createCharacteristic(BLE_CHAR_PASS, NIMBLE_PROPERTY::WRITE)->setCallbacks(new ProvCallbacks('p'));
  svc->createCharacteristic(BLE_CHAR_HOST, NIMBLE_PROPERTY::WRITE)->setCallbacks(new ProvCallbacks('h'));
  svc->createCharacteristic(BLE_CHAR_PORT, NIMBLE_PROPERTY::WRITE)->setCallbacks(new ProvCallbacks('o'));
  svc->createCharacteristic(BLE_CHAR_COMMIT, NIMBLE_PROPERTY::WRITE)->setCallbacks(new ProvCallbacks('c'));
  NimBLECharacteristic* info = svc->createCharacteristic(BLE_CHAR_INFO, NIMBLE_PROPERTY::READ);
  info->setValue((std::string("{\"device_id\":\"") + deviceId.c_str() + "\",\"fw\":\"" FIRMWARE_VERSION "\"}"));
  svc->start();
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(BLE_SERVICE_UUID);
  adv->setScanResponse(true);
  adv->start();
  Serial.printf("[ble] provisioning mode: advertising as '%s'. Write ssid/pass/host/port, then '1' to commit.\n", name.c_str());
  Serial.println("[ble] (serial console commands also work here; press EN to exit without saving)");

  while (!gCommit) {
    led.update();
    handleSerialConsole(cfg, deviceId, "PROVISIONING");
    delay(10);
  }
  Serial.println("[ble] commit received: saving and rebooting");
  saveNetConfig(cfg);
  delay(500);
  NimBLEDevice::deinit(true);
  ESP.restart();
}
#else
void runBleProvisioning(const String&, StatusLed&) {
  Serial.println("[ble] BLE provisioning disabled at build time; use the serial console.");
}
#endif
