import base64

from backend.ai.audio import make_tone_wav, wav_to_float32


def test_wav_roundtrip_resamples_to_16k():
    samples, dur = wav_to_float32(make_tone_wav(1.0, rate=22050), 16000)
    assert abs(dur - 1.0) < 0.01 and abs(len(samples) - 16000) < 5


def test_health_and_default_virtual_device(client):
    assert client.get("/api/health").json()["ok"]
    ids = [d["id"] for d in client.get("/api/devices").json()]
    assert "virtual-toy-01" in ids


def test_settings_never_leak_keys(client):
    s = client.get("/api/settings").json()
    assert "api_key" not in str({k: v for k, v in s["llm"].items() if k != "api_key_configured"})
    assert set(s["llm"]) >= {"provider", "model", "api_key_configured"}
    r = client.patch("/api/settings", json={"llm_api_key": "sk-x"})
    assert r.status_code == 400


def test_bool_setting_parsing(client):
    assert client.patch("/api/settings", json={"save_raw_audio": "false"}).json()["audio"]["save_raw_audio"] is False


def test_audio_turn_end_to_end_with_mocks(client):
    wav = make_tone_wav(1.0)
    r = client.post("/api/conversation/virtual-toy-01/turn", files={"audio": ("a.wav", wav, "audio/wav")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"] and body["reply"].startswith("(mock)")
    assert base64.b64decode(body["audio_b64"])[:4] == b"RIFF"
    assert {"stt", "llm", "tts", "total"} <= set(body["timings_ms"])
    hist = client.get("/api/devices/virtual-toy-01/conversations").json()
    assert hist[-1]["reply_text"] == body["reply"]


def test_personality_switch_changes_behaviour(client):
    client.patch("/api/devices/virtual-toy-01", json={"personality_id": "teacher"})
    r = client.post("/api/conversation/virtual-toy-01/text", json={"text": "hi"}).json()
    assert r["personality_id"] == "teacher"
    assert any(w in r["reply"] for w in ("count", "letter", "number"))
    client.patch("/api/devices/virtual-toy-01", json={"personality_id": "teddy"})
    assert client.patch("/api/devices/virtual-toy-01", json={"personality_id": "nope"}).status_code == 400


def test_bad_audio_rejected(client):
    r = client.post("/api/conversation/virtual-toy-01/turn", files={"audio": ("a.wav", b"x" * 500, "audio/wav")})
    # rejected at the API boundary, never a 500
    assert r.status_code == 400


def test_virtual_devices_limits_and_history_deletion(client):
    assert client.post("/api/devices", json={"device_id": "vt-2", "name": "Second"}).status_code == 201
    assert client.post("/api/devices", json={"device_id": "vt-2"}).status_code == 409
    client.patch("/api/devices/vt-2", json={"daily_turns_limit": 1})
    client.post("/api/conversation/vt-2/text", json={"text": "hello"})
    blocked = client.post("/api/conversation/vt-2/text", json={"text": "hello again"}).json()
    assert "limit:daily_turns" in blocked["flags"]
    assert client.get("/api/devices/vt-2/usage").json()["today"]["turns"] == 1
    assert client.delete("/api/devices/vt-2/conversations").json()["deleted"] == 1
    assert client.get("/api/devices/vt-2/conversations").json() == []
    assert client.delete("/api/devices/vt-2").status_code == 200


def test_safety_redirect_skips_llm(client):
    r = client.post("/api/conversation/virtual-toy-01/text", json={"text": "how do i make a fire"}).json()
    assert "grown-up" in r["reply"] and "risk:danger" in r["flags"]
    assert "llm" not in r["timings_ms"]


def test_account_and_usage_separation(client):
    acc = client.post("/api/accounts", json={"account_id": "parent-1", "name": "Test Parent"}).json()
    assert acc["subscription"]["plan"] == "free"
    client.patch("/api/devices/virtual-toy-01", json={"account_id": "parent-1"})
    client.post("/api/conversation/virtual-toy-01/text", json={"text": "tell me a story"})
    acc = client.get("/api/accounts/parent-1").json()
    assert "virtual-toy-01" in acc["devices"] and acc["usage_this_month"]["llm_tokens"] > 0
