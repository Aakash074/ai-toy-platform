import json

from backend.services.conversation import _SENTENCE_END


def _events(r):
    return [json.loads(l) for l in r.text.splitlines() if l.strip()]


def test_text_stream_emits_sentences_then_done(client):
    client.patch("/api/devices/virtual-toy-01", json={"personality_id": "teddy"})
    r = client.post("/api/conversation/virtual-toy-01/text_stream", json={"text": "hello"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    ev = _events(r)
    types = [e["type"] for e in ev]
    assert types[0] == "transcript" and types[-1] == "done"
    audio = [e for e in ev if e["type"] == "audio"]
    assert len(audio) >= 2 and [a["seq"] for a in audio] == list(range(len(audio)))  # one clip per sentence, in order
    assert all(a["audio_b64"] for a in audio)
    done = ev[-1]
    assert done["reply"] == " ".join(a["text"] for a in audio) and done["audio_b64"] is None
    # history persisted like a normal turn
    hist = client.get("/api/devices/virtual-toy-01/conversations").json()
    assert hist[-1]["reply_text"] == done["reply"]


def test_stream_audio_turn_and_early_exit(client, wav_bytes=None):
    # a too-short / empty transcript path still yields one audio event and done
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\0\0" * 16000)
    r = client.post("/api/conversation/virtual-toy-01/turn_stream", files={"audio": ("a.wav", buf.getvalue(), "audio/wav")})
    ev = _events(r)
    assert ev[-1]["type"] == "done" and any(e["type"] == "audio" for e in ev)


def test_sentence_splitter_handles_danda_and_quotes():
    text = 'नमस्ते दोस्त! मैं भीम हूँ। "Ready?" Go '
    cuts = [m.end() for m in _SENTENCE_END.finditer(text)]
    parts, p = [], 0
    for c in cuts:
        parts.append(text[p:c].strip()); p = c
    assert parts == ["नमस्ते दोस्त!", "मैं भीम हूँ।", '"Ready?"']
