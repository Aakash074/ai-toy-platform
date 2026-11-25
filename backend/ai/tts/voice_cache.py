"""Pre-made voice lines: recorded once with an expressive (slow / rate-limited) engine, replayed instantly.

Cache key = normalized text + the personality's recording settings (clone or gemini_tts), so changing the voice or style
naturally invalidates old recordings. Files live in data/voice_cache/<sha1>.mp3 (local only).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

import yaml

from backend.ai.base import SpeechAudio, TTSProvider

log = logging.getLogger("ai.voicecache")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def recording_settings(voice: dict) -> dict:
    """Which pre-recording engine/settings a personality uses: a consented voice clone, else Gemini."""
    v = voice or {}
    if v.get("clone"):
        return {"engine": "clone", **v["clone"]}
    return v.get("gemini_tts") or {}


def cache_key(text: str, voice: dict) -> str:
    g = recording_settings(voice)
    raw = json.dumps({"t": _norm(text), "g": g}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class VoiceCache:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, text: str, voice: dict) -> Path:
        return self.dir / f"{cache_key(text, voice)}.mp3"

    def get(self, text: str, voice: dict) -> bytes | None:
        p = self.path(text, voice)
        return p.read_bytes() if p.exists() else None

    def put(self, text: str, voice: dict, data: bytes) -> None:
        p = self.path(text, voice)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)


class CachedTTS(TTSProvider):
    """Serves a recorded performance when the exact line exists in the cache; otherwise the live engine."""

    def __init__(self, inner: TTSProvider, cache: VoiceCache):
        self.inner, self.cache = inner, cache
        self.name = inner.name

    async def synthesize(self, text: str, voice: dict) -> SpeechAudio:
        data = self.cache.get(text, voice)
        if data:
            log.info("[voice] using pre-recorded line (%d chars)", len(text))
            return SpeechAudio(data=data, mime="audio/mpeg", meta={"engine": "voice_cache"})
        return await self.inner.synthesize(text, voice)

    async def warmup(self, *a, **k):  # pragma: no cover - passthrough if present
        if hasattr(self.inner, "warmup"):
            return await self.inner.warmup(*a, **k)


class VoiceLines:
    """config/voice_lines.yaml: fixed lines + short stories per personality."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        self.reload()

    def reload(self) -> None:
        try:
            self.data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            self.data = {}

    def line(self, personality_id: str, key: str) -> str | None:
        v = (self.data.get(personality_id) or {}).get(key)
        return _norm(v) if isinstance(v, str) else None

    def stories(self, personality_id: str) -> dict[str, dict]:
        return {k: {"title": v.get("title", k), "text": _norm(v.get("text", ""))}
                for k, v in ((self.data.get(personality_id) or {}).get("stories") or {}).items()}

    def all_texts(self, personality_id: str) -> list[str]:
        p = self.data.get(personality_id) or {}
        out = [_norm(v) for k, v in p.items() if isinstance(v, str)]
        out += [s["text"] for s in self.stories(personality_id).values()]
        return [t for t in out if t]
