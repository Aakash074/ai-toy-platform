"""Speech-to-text adapters.

- faster_whisper : local, offline, free (default). Model downloads once (~150MB for base.en).
                   Non-English personalities (hi, te, ...) switch to STT_MULTILINGUAL_MODEL
                   (default "large-v3-turbo", ~1.6GB) with the personality's language as a hint.
- mlx_whisper    : local, offline, free, Apple Silicon GPU (Metal). Same Whisper large-v3-turbo
                   model for every language, ~1 s per turn on an M-series Mac (vs ~6 s on CPU).
                   Model: STT_MLX_MODEL (default mlx-community/whisper-large-v3-turbo, ~1.6GB once).
- openai_compat  : any OpenAI-compatible /audio/transcriptions endpoint
                   (OpenAI, Groq's free tier, a local whisper server...).
- mock           : returns a fixed transcript; for tests and demos without a mic.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from backend.ai.audio import AudioFormatError, wav_to_float32
from backend.ai.base import ProviderError, STTProvider, Transcript

log = logging.getLogger("ai.stt")


_REPEAT = re.compile(r"(.{2,12}?)\1{3,}")


def collapse_repeats(text: str) -> str:
    """Whisper sometimes loops on a syllable ("ద్యాంద్యాంద్యాం..."), esp. in Telugu. Keep one copy."""
    return _REPEAT.sub(r"\1", text)


def normalize_language(language: str | None) -> str | None:
    """'hi-IN' / 'HI' -> 'hi'. None/'' -> None (auto-detect)."""
    if not language:
        return None
    return language.strip().lower().replace("_", "-").split("-")[0] or None


class FasterWhisperSTT(STTProvider):
    """Local Whisper. The language hint picks the model:
    - English (or no hint) -> `model_size` (default base.en, English-only, ~150 MB).
    - any other language   -> `multilingual_model` (default large-v3-turbo, ~1.6 GB) when `model_size` is an
      English-only *.en model; otherwise `model_size` itself. The language is passed to Whisper so it
      transcribes in that language/script instead of guessing. Models load lazily, once each.
    """

    name = "faster_whisper"

    def __init__(self, model_size: str = "base.en", multilingual_model: str = "large-v3-turbo"):
        self.model_size = model_size
        self.multilingual_model = multilingual_model or "large-v3-turbo"
        self._models: dict[str, object] = {}
        self._load_lock = asyncio.Lock()
        self._load_tasks: dict[str, asyncio.Task] = {}

    def model_for(self, language: str | None) -> str:
        lang = normalize_language(language)
        if lang and lang != "en" and self.model_size.endswith(".en"):
            return self.multilingual_model
        return self.model_size

    def _load(self, size: str):
        from faster_whisper import WhisperModel  # imported lazily: heavy

        t = time.perf_counter()
        model = WhisperModel(size, device="cpu", compute_type="int8")
        log.info("faster-whisper '%s' loaded in %.1fs", size, time.perf_counter() - t)
        return model

    async def _get_model(self, size: str):
        # The load runs as a shielded task so a caller timing out never discards a half-finished
        # download/load; the next caller simply awaits the same task.
        async with self._load_lock:
            if size in self._models:
                return self._models[size]
            task = self._load_tasks.get(size)
            if task is None or (task.done() and task.exception()):
                task = self._load_tasks[size] = asyncio.create_task(asyncio.to_thread(self._load, size))
        model = await asyncio.shield(task)
        self._models[size] = model
        return model

    async def warmup(self, language: str | None = None) -> None:
        await self._get_model(self.model_for(language))

    async def transcribe(self, wav_bytes: bytes, language: str | None = None) -> Transcript:
        try:
            samples, duration = wav_to_float32(wav_bytes, 16000)
        except AudioFormatError as e:
            raise ProviderError("stt", str(e))
        size = self.model_for(language)
        model = await self._get_model(size)
        lang = None if size.endswith(".en") else normalize_language(language)

        def _run():
            segments, info = model.transcribe(
                samples, language=lang, beam_size=1, vad_filter=True,
                condition_on_previous_text=False,
            )
            return collapse_repeats(" ".join(s.text.strip() for s in segments).strip()), info.language

        try:
            text, detected = await asyncio.to_thread(_run)
        except Exception as e:  # model/runtime errors
            raise ProviderError("stt", f"faster-whisper ({size}) failed: {e}")
        return Transcript(text=text, language=detected, audio_seconds=duration)


class MlxWhisperSTT(STTProvider):
    """Whisper on the Apple GPU via MLX. One multilingual model; the personality language is a hint."""
    name = "mlx_whisper"

    def __init__(self, repo: str = "mlx-community/whisper-large-v3-turbo"):
        import mlx_whisper  # noqa: F401  (fail at startup if missing / not Apple Silicon)
        self.repo = repo or "mlx-community/whisper-large-v3-turbo"
        self._lock = asyncio.Lock()  # MLX/Metal work is serialized; turns are per-device-locked anyway
        self._warm = False

    def _run(self, samples, lang):
        import mlx_whisper
        # temperature=0 only: the default 0.2..1.0 retry ladder re-decodes looping output up to 5x (slow).
        out = mlx_whisper.transcribe(samples, path_or_hf_repo=self.repo, language=lang, temperature=0.0,
                                     condition_on_previous_text=False, verbose=None)
        return collapse_repeats(out.get("text", "").strip()), out.get("language") or lang

    async def warmup(self, language: str | None = None) -> None:
        if self._warm:
            return
        import numpy as np
        t = time.monotonic()
        async with self._lock:
            await asyncio.to_thread(self._run, np.zeros(16000, dtype=np.float32), "en")
        self._warm = True
        log.info("mlx-whisper '%s' ready in %.1fs", self.repo, time.monotonic() - t)

    async def transcribe(self, wav_bytes: bytes, language: str | None = None) -> Transcript:
        try:
            samples, duration = wav_to_float32(wav_bytes, 16000)
        except AudioFormatError as e:
            raise ProviderError("stt", str(e))
        lang = normalize_language(language)
        try:
            async with self._lock:
                text, detected = await asyncio.to_thread(self._run, samples, lang)
        except Exception as e:
            raise ProviderError("stt", f"mlx-whisper failed: {e}")
        return Transcript(text=text, language=detected, audio_seconds=duration)


class OpenAICompatSTT(STTProvider):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float):
        self.base_url, self.api_key, self.model, self.timeout_s = base_url.rstrip("/"), api_key, model, timeout_s

    async def transcribe(self, wav_bytes: bytes, language: str | None = None) -> Transcript:
        _, duration = wav_to_float32(wav_bytes, 16000)
        data = {"model": self.model}
        lang = normalize_language(language)
        if lang:
            data["language"] = lang  # ISO-639-1 hint, e.g. en / hi / te
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers=headers, data=data,
                    files={"file": ("speech.wav", wav_bytes, "audio/wav")},
                )
        except httpx.TimeoutException:
            raise ProviderError("stt", "transcription request timed out", retryable=True)
        except httpx.HTTPError as e:
            raise ProviderError("stt", f"network error: {e}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError("stt", f"HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500 or r.status_code == 429)
        return Transcript(text=r.json().get("text", "").strip(), language=lang, audio_seconds=duration)


class MockSTT(STTProvider):
    name = "mock"

    TEXTS = {
        "en": "Hello! Can you tell me a story?",
        "hi": "नमस्ते! क्या तुम मुझे एक कहानी सुनाओगे?",
        "te": "నమస్తే! నాకు ఒక కథ చెబుతావా?",
    }

    def __init__(self, text: str | None = None):
        self.text = text

    async def transcribe(self, wav_bytes: bytes, language: str | None = None) -> Transcript:
        _, duration = wav_to_float32(wav_bytes, 16000)
        lang = normalize_language(language) or "en"
        text = self.text or self.TEXTS.get(lang, self.TEXTS["en"])
        return Transcript(text=text, language=lang, audio_seconds=duration)


def build_stt(s) -> STTProvider:
    p = s.stt_provider
    if p == "faster_whisper":
        return FasterWhisperSTT(s.stt_model or "base.en", getattr(s, "stt_multilingual_model", "large-v3-turbo"))
    if p == "mlx_whisper":
        return MlxWhisperSTT(getattr(s, "stt_mlx_model", "") or "mlx-community/whisper-large-v3-turbo")
    if p == "openai_compat":
        model = s.stt_model if "whisper" in (s.stt_model or "") else "whisper-1"
        return OpenAICompatSTT(s.stt_base_url, s.stt_api_key, model, s.stt_timeout_s)
    if p == "mock":
        return MockSTT()
    raise ValueError(f"Unknown STT_PROVIDER '{p}' (use faster_whisper | mlx_whisper | openai_compat | mock)")
