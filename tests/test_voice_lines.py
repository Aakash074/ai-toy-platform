import json

from backend.ai.base import SpeechAudio
from backend.ai.tts.voice_cache import CachedTTS, VoiceCache, VoiceLines, cache_key


class _Inner:
    name = "inner"

    async def synthesize(self, text, voice):
        return SpeechAudio(data=b"LIVE", mime="audio/wav", meta={})


def test_cached_line_served_and_key_depends_on_voice(tmp_path):
    import asyncio
    cache = VoiceCache(tmp_path)
    v1 = {"gemini_tts": {"voice": "Leda"}}
    cache.put("नमस्ते  दोस्त!", v1, b"REC")
    tts = CachedTTS(_Inner(), cache)
    assert asyncio.run(tts.synthesize("नमस्ते दोस्त!", v1)).data == b"REC"          # whitespace-normalized
    assert asyncio.run(tts.synthesize("नमस्ते दोस्त!", {"gemini_tts": {"voice": "Puck"}})).data == b"LIVE"
    assert cache_key("a", v1) != cache_key("b", v1)


def test_voice_lines_file_has_bheem_lines_and_stories():
    vl = VoiceLines("config/voice_lines.yaml")
    for pid in ("bheem_hi", "bheem_te"):
        assert vl.line(pid, "greeting") and vl.line(pid, "not_heard")
        assert {"jaggu_mango", "chutki_laddoo", "rain_picnic"} <= set(vl.stories(pid))


def test_greet_endpoint(client):
    client.patch("/api/devices/virtual-toy-01", json={"personality_id": "bheem_hi"})
    r = client.post("/api/conversation/virtual-toy-01/greet").json()
    assert "छोटा भीम" in r["text"] and r["language"] == "hi"
    client.patch("/api/devices/virtual-toy-01", json={"personality_id": "teddy"})


def test_story_tag_plays_recorded_story(client, monkeypatch):
    from backend.ai.llm.providers import MockLLM
    from backend.ai.base import LLMResult

    async def chat(self, system, messages, max_tokens):
        assert "RECORDED STORIES" in system
        return LLMResult(text="चलो, एक मज़ेदार कहानी सुनो! <<story=jaggu_mango>>")
    monkeypatch.setattr(MockLLM, "chat", chat)
    client.patch("/api/devices/virtual-toy-01", json={"personality_id": "bheem_hi"})
    r = client.post("/api/conversation/virtual-toy-01/text_stream", json={"text": "कहानी सुनाओ"})
    ev = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    audio = [e for e in ev if e["type"] == "audio"]
    assert audio[0]["text"].startswith("चलो") and "जग्गू बंदर" in audio[-1]["text"]
    assert "story:jaggu_mango" in ev[-1]["flags"] and "<<" not in ev[-1]["reply"]
    client.patch("/api/devices/virtual-toy-01", json={"personality_id": "teddy"})
