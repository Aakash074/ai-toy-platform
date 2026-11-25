"""Hindi / Telugu support: STT language routing, guard on Indic text, localized mock replies."""
import asyncio
import re

from backend.ai.audio import make_tone_wav
from backend.ai.llm.providers import MockLLM
from backend.ai.stt.providers import FasterWhisperSTT, OpenAICompatSTT, normalize_language
from backend.personalities.registry import Personality
from backend.safety import guard

DEVANAGARI = re.compile("[ऀ-ॿ]")
TELUGU = re.compile("[ఀ-౿]")


def test_whisper_model_follows_language():
    stt = FasterWhisperSTT("base.en", "small")
    assert stt.model_for(None) == "base.en"
    assert stt.model_for("en") == "base.en"
    assert stt.model_for("hi") == "small"
    assert stt.model_for("te-IN") == "small"
    # a multilingual primary model is used for every language
    assert FasterWhisperSTT("small", "medium").model_for("te") == "small"
    assert normalize_language("hi_IN") == "hi" and normalize_language("") is None


def test_whisper_passes_language_hint_to_multilingual_model():
    stt = FasterWhisperSTT("base.en", "small")
    seen = {}

    class FakeModel:
        def __init__(self, name):
            self.name = name

        def transcribe(self, samples, language=None, **kw):
            seen[self.name] = language
            return iter([]), type("Info", (), {"language": language or "en"})()

    stt._load = lambda size: FakeModel(size)  # no download in tests
    asyncio.run(stt.transcribe(make_tone_wav(0.5), "te"))
    asyncio.run(stt.transcribe(make_tone_wav(0.5), "en"))
    assert seen == {"small": "te", "base.en": None}  # .en models take no language arg


def test_openai_compat_stt_sends_language(monkeypatch):
    import httpx

    sent = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"text": "नमस्ते"}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, data=None, files=None):
            sent.update(data)
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    stt = OpenAICompatSTT("http://x/v1", "", "whisper-1", 5)
    tr = asyncio.run(stt.transcribe(make_tone_wav(0.5), "hi-IN"))
    assert sent["language"] == "hi" and tr.text == "नमस्ते" and tr.language == "hi"


def test_guard_keeps_indic_text_and_splits_on_danda():
    hi = "**नमस्ते!** मैं गप्पू हूँ। तुम कैसे हो? चलो खेलते हैं। एक, दो, तीन।"
    r = guard.check_output(hi, 3, "hi")
    assert r.text == "नमस्ते! मैं गप्पू हूँ। तुम कैसे हो?" and "trimmed" in r.flags
    te = "నమస్తే! నేను గప్పు. నీకు ఏ రంగు ఇష్టం? మనం ఆడుకుందాం!"
    r = guard.check_output(te, 3, "te")
    assert r.text == "నమస్తే! నేను గప్పు. నీకు ఏ రంగు ఇష్టం?" and TELUGU.search(r.text)
    assert guard.check_output("पोम-पोम-पम! चलो गिनती करें।", 3, "hi").flags == []


def test_guard_blocks_indic_unsafe_words_with_localized_fallback():
    r = guard.check_output("चलो बंदूक से खेलें।", 3, "hi")
    assert r.flags == ["blocked_content"] and r.text == guard.FALLBACK_REPLIES["hi"]
    r = guard.check_output("తుపాకీ తో ఆడుకుందాం.", 3, "te")
    assert r.flags == ["blocked_content"] and r.text == guard.FALLBACK_REPLIES["te"]
    # English behaviour unchanged
    assert guard.check_output("Let's play with a gun!", 3).text == guard.FALLBACK_REPLY


def test_system_prompt_language_rules_after_safety():
    p = Personality(id="gappu_te", name="Gappu", system_prompt="Be fun.", language="te")
    sp = guard.build_system_prompt(p)
    assert sp.startswith("SAFETY RULES") and "Telugu script" in sp
    assert sp.index("SAFETY RULES") < sp.index("Telugu script")


def test_mock_llm_replies_in_personality_language():
    llm = MockLLM()
    for pid, lang, script in (("gappu_hi", "hi", DEVANAGARI), ("gappu_te", "te", TELUGU)):
        sp = guard.build_system_prompt(Personality(id=pid, name="G", system_prompt="x", language=lang))
        assert script.search(asyncio.run(llm.chat(sp, [], 100)).text)
    # unknown personality id, language-only routing
    sp = guard.build_system_prompt(Personality(id="other", name="O", system_prompt="x", language="hi"))
    assert DEVANAGARI.search(asyncio.run(llm.chat(sp, [], 100)).text)


def test_gappu_personalities_turns(client):
    pers = {p["id"]: p for p in client.get("/api/personalities").json()}
    assert pers["gappu_hi"]["language"] == "hi" and pers["gappu_te"]["language"] == "te"
    assert pers["gappu_hi"]["voice"]["macos_say"]["voice"] == "Lekha"
    try:
        for pid, script in (("gappu_hi", DEVANAGARI), ("gappu_te", TELUGU)):
            assert client.patch("/api/devices/virtual-toy-01", json={"personality_id": pid}).status_code == 200
            r = client.post("/api/conversation/virtual-toy-01/turn",
                            files={"audio": ("a.wav", make_tone_wav(1.0), "audio/wav")}).json()
            assert r["personality_id"] == pid and r["language"] == pid[-2:]
            assert script.search(r["transcript"]) and script.search(r["reply"])  # mock STT follows language
    finally:
        client.patch("/api/devices/virtual-toy-01", json={"personality_id": "teddy"})


def test_say_does_not_respeak_indic_text_with_english_voice(monkeypatch):
    import shutil

    import pytest

    from backend.ai.base import ProviderError
    from backend.ai.tts.providers import MacSayTTS

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/say")
    tts = MacSayTTS()
    calls = []

    async def fake_say(text, voice, rate):
        calls.append(voice)
        if voice != MacSayTTS.DEFAULT_VOICE:
            raise ProviderError("tts", "voice not installed")
        return b"RIFF"

    monkeypatch.setattr(tts, "_say", fake_say)
    with pytest.raises(ProviderError):  # → tts_failed → browser speech fallback in hi-IN
        asyncio.run(tts.synthesize("नमस्ते", {"macos_say": {"voice": "Lekha"}}))
    assert calls == ["Lekha"]
    # English text still falls back to the default voice as before
    assert asyncio.run(tts.synthesize("Hello", {"macos_say": {"voice": "Nope"}})).data == b"RIFF"


def test_bheem_personalities_reply_in_their_language(client):
    pers = {p["id"]: p for p in client.get("/api/personalities").json()}
    assert pers["bheem_hi"]["language"] == "hi" and pers["bheem_te"]["language"] == "te"
    for pid, script in (("bheem_hi", DEVANAGARI), ("bheem_te", TELUGU)):
        client.patch("/api/devices/virtual-toy-01", json={"personality_id": pid})
        r = client.post("/api/conversation/virtual-toy-01/text", json={"text": "hello"}).json()
        assert script.search(r["reply"]), r
