"""Device WebSocket protocol tests (same protocol the ESP32 firmware and the simulator speak)."""
import json


def _hello(ws, fw="0.1.0"):
    ws.send_text(json.dumps({"type": "hello", "firmware": fw, "hardware": "esp32", "mac": "AA:BB", "ip": "10.0.0.9"}))
    welcome = json.loads(ws.receive_text())
    assert welcome["type"] == "welcome" and welcome["heartbeat_interval_s"] > 0
    assert json.loads(ws.receive_text()) == {"type": "set_state", "state": "CONNECTED"}
    return welcome


def _next(dash, type_):
    for _ in range(20):
        e = json.loads(dash.receive_text())
        if e["type"] == type_:
            return e
    raise AssertionError(f"no {type_} event")


def test_register_heartbeat_button_and_state_commands(client):
    with client.websocket_connect("/ws/dashboard") as dash:
        assert json.loads(dash.receive_text())["type"] == "hello"
        with client.websocket_connect("/ws/device/esp32-test01") as dev:
            _hello(dev)
            assert _next(dash, "device_online")["device_id"] == "esp32-test01"
            d = client.get("/api/devices/esp32-test01").json()
            assert d["online"] and d["firmware_version"] == "0.1.0" and not d["is_virtual"]

            dev.send_text(json.dumps({"type": "heartbeat", "rssi": -61, "uptime_s": 42, "free_heap": 150000, "state": "CONNECTED"}))
            assert _next(dash, "heartbeat")["rssi"] == -61

            # ESP32 button → dashboard
            dev.send_text(json.dumps({"type": "button", "action": "press"}))
            assert _next(dash, "button")["action"] == "press"

            # dashboard starts recording → backend tells the device to show LISTENING
            dash.send_text(json.dumps({"type": "recording_started", "device_id": "esp32-test01"}))
            assert json.loads(dev.receive_text()) == {"type": "set_state", "state": "LISTENING"}

            # a conversation turn drives THINKING → SPEAKING on the device LED
            client.post("/api/conversation/esp32-test01/text", json={"text": "hello"})
            states = [json.loads(dev.receive_text())["state"] for _ in range(2)]
            assert states == ["THINKING", "SPEAKING"]

            # personality change is pushed to the device
            client.patch("/api/devices/esp32-test01", json={"personality_id": "teacher"})
            assert json.loads(dev.receive_text())["type"] == "personality"

            # identify command
            assert client.post("/api/devices/esp32-test01/command", json={"type": "identify"}).json()["sent"]
            assert json.loads(dev.receive_text())["type"] == "identify"
        # socket closed → offline
        assert _next(dash, "device_offline")["device_id"] == "esp32-test01"
    assert client.get("/api/devices/esp32-test01").json()["online"] is False


def test_reconnect_replaces_stale_socket(client):
    with client.websocket_connect("/ws/device/esp32-test02") as old:
        _hello(old)
        with client.websocket_connect("/ws/device/esp32-test02") as new:  # device rebooted
            _hello(new, fw="0.1.1")
            assert client.get("/api/devices/esp32-test02").json()["firmware_version"] == "0.1.1"
            assert client.post("/api/devices/esp32-test02/command", json={"type": "ping"}).json()["sent"]
            assert json.loads(new.receive_text())["type"] == "ping"
            assert client.get("/api/devices/esp32-test02").json()["online"] is True


def test_bad_handshake_rejected(client):
    with client.websocket_connect("/ws/device/esp32-test03") as ws:
        ws.send_text(json.dumps({"type": "heartbeat"}))
        try:
            ws.receive_text()
            raise AssertionError("should have been closed")
        except Exception:
            pass
    assert client.get("/api/devices/esp32-test03").status_code == 404


def test_ota_placeholder(client):
    with client.websocket_connect("/ws/device/esp32-test04") as ws:
        _hello(ws)
    r = client.get("/api/ota/esp32-test04?version=0.1.0").json()
    assert r["update_available"] is False
