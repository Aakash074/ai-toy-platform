"""Text-to-speech adapters.

- macos_say     : macOS built-in `say` → WAV. Offline, free, zero setup on a Mac (default).
- openai_compat : any OpenAI-compatible /audio/speech endpoint.
- edge_tts      : Microsoft Edge neural voices (free, no key, online; unofficial endpoint).
                  Natural Hindi/Telugu/English voices with pitch/rate control, used for the
                  cartoon-style character voices. Falls back to macos_say if offline/unavailable.
- mock          : a short beep; for tests.

Voice config comes from the personality, e.g.
    voice: {macos_say: {voice: Samantha, rate: 175}, openai_compat: {voice: nova}}

Hindi / Telugu: macOS ships "Lekha" (hi_IN) and "Geeta" (te_IN) — check `say -v '?'`. If the
configured voice is missing, non-Latin text is NOT re-spoken with the English default voice
(it would be gibberish); the turn reports tts_failed and the dashboard falls back to the
browser's speechSynthesis in the personality's language.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

import httpx

from backend.ai.audio import make_tone_wav
from backend.ai.base import ProviderError, SpeechAudio, TTSProvider

log = logging.getLogger("ai.tts")


def _has_non_latin_letters(text: str) -> bool:
    return any(ch.isalpha() and ord(ch) > 0x24F for ch in text)


class MacSayTTS(TTSProvider):
    name = "macos_say"
    DEFAULT_VOICE = "Samantha"

    def __init__(self, timeout_s: float = 30.0):
        if not shutil.which("say"):
            raise RuntimeError("macos_say TTS needs macOS `say` (set TTS_PROVIDER to another provider)")
        self.timeout_s = timeout_s

    async def _say(self, text: str, voice: str, rate: int) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.wav"
            # Text goes via stdin (-f -) so it is never interpreted as CLI flags.
            proc = await asyncio.create_subprocess_exec(
                "say", "-v", voice, "-r", str(rate), "--data-format=LEI16@22050", "-o", str(out), "-f", "-",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(text.encode("utf-8")), self.timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                raise ProviderError("tts", "macOS say timed out", retryable=True)
            if proc.returncode != 0 or not out.exists():
                raise ProviderError("tts", f"say failed ({proc.returncode}): {err.decode(errors='ignore')[:200]}")
            return out.read_bytes()

    async def synthesize(self, text: str, voice: dict) -> SpeechAudio:
        cfg = voice.get("macos_say", {}) if voice else {}
        v, rate = cfg.get("voice", self.DEFAULT_VOICE), int(cfg.get("rate", 175))
        try:
            data = await self._say(text, v, rate)
        except ProviderError as e:
            if v == self.DEFAULT_VOICE or _has_non_latin_letters(text):
                raise
            log.warning("voice '%s' failed (%s); falling back to %s", v, e.message, self.DEFAULT_VOICE)
            v = self.DEFAULT_VOICE
            data = await self._say(text, v, rate)
        return SpeechAudio(data=data, mime="audio/wav", meta={"voice": v, "rate": rate})


class OpenAICompatTTS(TTSProvider):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float):
        self.base_url, self.api_key, self.model, self.timeout_s = base_url.rstrip("/"), api_key, model, timeout_s

    async def synthesize(self, text: str, voice: dict) -> SpeechAudio:
        cfg = voice.get("openai_compat", {}) if voice else {}
        payload = {"model": self.model, "input": text, "voice": cfg.get("voice", "alloy"),
                   "response_format": "wav", "speed": float(cfg.get("speed", 1.0))}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                r = await client.post(f"{self.base_url}/audio/speech", json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ProviderError("tts", "speech request timed out", retryable=True)
        except httpx.HTTPError as e:
            raise ProviderError("tts", f"network error: {e}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError("tts", f"HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
        return SpeechAudio(data=r.content, mime=r.headers.get("content-type", "audio/wav"), meta={"voice": payload["voice"]})


# Written laughs / thinking sounds that voice engines spell out instead of performing.
# Token-based (not \b regex): Indic vowel signs/virama are combining marks, which break word boundaries.
_NONVERBAL_LATIN = re.compile(r"^(?:(?:he|hi|ha|ho|hu){2,}h?|h+m+|u+m+|o+h{2,}|a+h{2,})$", re.IGNORECASE)
_NONVERBAL_WORDS = {
    "हेहे", "हेहेहे", "हाहा", "हाहाहा", "हीही", "हा", "हे", "ही", "हम्म", "हम्म्म", "हम", "ओह्ह", "ओह्ह्ह", "आह्ह",
    "హహ్హా", "హహ్హ", "హహహ", "హహహా", "హిహి", "హిహిహి", "హీహీ", "హ్మ్", "హ్మ్మ్", "హమ్మ్", "హా", "హి", "హహ", "ఓహ్హ్",
}
_TOKEN = re.compile(r"(\s+)")


def _is_nonverbal(word: str) -> bool:
    w = word.strip(".,!?।॥…'\"-").lower()
    return bool(w) and (w in _NONVERBAL_WORDS or bool(_NONVERBAL_LATIN.match(w)))


def speakable(text: str) -> str:
    """Text for the voice engine: drop laughs/hmm sounds, turn ellipses into a light pause (comma)."""
    t = re.sub(r"\s*(?:\.{2,}|…)\s*", ", ", text)
    words = [w for w in _TOKEN.split(t) if w and not w.isspace()]
    kept = []
    for w in words:
        if _is_nonverbal(w):
            # keep sentence-ending punctuation that was attached to the dropped sound
            end = w.rstrip()[-1:] if w.rstrip()[-1:] in "!?।." else ""
            if end and kept:
                kept[-1] = kept[-1].rstrip(",") + end
            continue
        kept.append(w)
    t = " ".join(kept)
    t = re.sub(r"\s*,\s*([,!?.।])", r"\1", t)
    t = re.sub(r"([!?.।])\1+", r"\1", t)
    t = re.sub(r"^[\s,!.।]+", "", t).strip().rstrip(",")
    return t


async def _shape_voice(parts: list[bytes], cfg: dict) -> bytes:
    """Join sentence clips naturally + cartoon-kid timbre, with ffmpeg.

    Each clip's leading/trailing silence (the engine pads ~0.3-0.5 s) is trimmed and replaced by a short
    breath gap (gap_ms, default 320) so sentences flow like real speech. kid_shift (e.g. 1.3) raises pitch
    AND formants together (resample trick), which is what makes a voice sound like a small child rather
    than an adult speaking high; tempo is restored afterwards. Loudness is normalized.
    """
    shift = float(cfg.get("kid_shift") or 1.0)
    tempo = float(cfg.get("tempo") or 1.0)
    gap = max(0, int(cfg.get("gap_ms", 320))) / 1000
    trim = "silenceremove=start_periods=1:start_threshold=-42dB:start_silence=0.03"
    # Pauses only between sentences (gap_ms); within a sentence the engine's comma pauses are cut to ~30 ms.
    inner = max(0.02, int(cfg.get("inner_pause_ms", 30)) / 1000)
    squeeze = f"silenceremove=stop_periods=-1:stop_duration={inner:.3f}:stop_threshold=-40dB:stop_silence={inner:.3f}"
    with tempfile.TemporaryDirectory() as td:
        args, chains = [], []
        for i, b in enumerate(parts):
            f = Path(td) / f"p{i}.mp3"
            f.write_bytes(b)
            args += ["-i", str(f)]
            pad = f",apad=pad_dur={gap:.3f}" if i < len(parts) - 1 else ""
            # inside a sentence: squeeze comma/word pauses to almost nothing (continuous speech)
            chains.append(f"[{i}:a]{trim},areverse,{trim},areverse,{squeeze}{pad}[a{i}]")
        joined = "".join(f"[a{i}]" for i in range(len(parts)))
        post = (f"asetrate={int(24000 * shift)},aresample=24000,"
                f"atempo={max(0.5, min(2.0, tempo / shift)):.4f},loudnorm=I=-16")
        graph = ";".join(chains) + f";{joined}concat=n={len(parts)}:v=0:a=1,{post}[out]"
        out = Path(td) / "out.mp3"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", *args, "-filter_complex", graph, "-map", "[out]",
            "-c:a", "libmp3lame", "-b:a", "64k", str(out),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(err.decode(errors="ignore")[:300])
        return out.read_bytes()


_SENTENCES = re.compile(r"(?<=[.!?।॥])\s+")


class EdgeTTS(TTSProvider):
    """Neural voices via the `edge-tts` package. Only the reply TEXT is sent (never the child's audio).

    Personality config:  voice: {edge_tts: {voice: te-IN-MohanNeural, pitch: "+35Hz", rate: "+12%"}}
    Raising pitch on a young male voice + a slightly faster rate gives a cartoon-kid sound.
    """
    name = "edge_tts"

    def __init__(self, timeout_s: float, fallback: TTSProvider | None):
        import edge_tts  # noqa: F401  (fail at startup if the package is missing)
        self.timeout_s, self.fallback = timeout_s, fallback

    async def _edge(self, text: str, cfg: dict) -> bytes:
        # One request for the whole reply: natural prosody and no artificial gaps between sentences.
        text = speakable(text) or text
        # Sentences in parallel (ready when the longest is); each request is hedged against slow responses.
        parts = [p for p in _SENTENCES.split(text.strip()) if p.strip()] or [text]
        audio = list(await asyncio.gather(*(self._hedged(p, cfg) for p in parts)))
        if shutil.which("ffmpeg"):
            try:
                return await asyncio.wait_for(_shape_voice(list(audio), cfg), 10)
            except Exception as e:  # never lose the reply over post-processing
                log.warning("voice shaping failed (%s); using plain voice", e)
        return b"".join(audio)

    async def _hedged(self, text: str, cfg: dict, after_s: float = 1.0) -> bytes:
        """The free endpoint is usually ~1 s but sometimes stalls for 5+ s: if the first request is slow,
        fire a second one and use whichever finishes first."""
        first = asyncio.create_task(self._edge_one(text, cfg))
        done, _ = await asyncio.wait({first}, timeout=after_s)
        if done:
            return first.result()
        second = asyncio.create_task(self._edge_one(text, cfg))
        pending = {first, second}
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if not t.exception():
                        return t.result()
            raise next(iter(done)).exception()
        finally:
            for t in pending:
                t.cancel()

    async def _edge_one(self, text: str, cfg: dict) -> bytes:
        import edge_tts
        com = edge_tts.Communicate(text, cfg["voice"], rate=cfg.get("rate", "+0%"),
                                   pitch=cfg.get("pitch", "+0Hz"), volume=cfg.get("volume", "+0%"))
        chunks = []
        async for part in com.stream():
            if part["type"] == "audio":
                chunks.append(part["data"])
        if not chunks:
            raise ProviderError("tts", "edge-tts returned no audio", retryable=True)
        return b"".join(chunks)

    async def synthesize(self, text: str, voice: dict) -> SpeechAudio:
        cfg = (voice or {}).get("edge_tts")
        if cfg and cfg.get("voice"):
            try:
                data = await asyncio.wait_for(self._edge(text, cfg), self.timeout_s)
                return SpeechAudio(data=data, mime="audio/mpeg", meta={"voice": cfg["voice"], "pitch": cfg.get("pitch")})
            except asyncio.TimeoutError:
                err = "timed out"
            except ProviderError as e:
                err = e.message
            except Exception as e:  # network / service errors from the unofficial endpoint
                err = f"{type(e).__name__}: {e}"
            if not self.fallback:
                raise ProviderError("tts", f"edge-tts failed: {err}", retryable=True)
            log.warning("edge-tts failed (%s); falling back to %s", err, self.fallback.name)
        if not self.fallback:
            raise ProviderError("tts", "personality has no edge_tts voice configured")
        return await self.fallback.synthesize(text, voice)


class GeminiTTS(TTSProvider):
    """Google Gemini speech generation: the voice is *directed* with a natural-language style note, so it
    can act (excitement, playfulness) instead of reading neutrally. Hindi and Telugu supported.

    Personality config:
        voice: {gemini_tts: {voice: Puck, style: "Say this like an excited, cheerful little cartoon boy"}}
    Output is 24 kHz 16-bit PCM, trimmed of edge silence and encoded to MP3. Any failure falls back to the
    next provider (edge_tts), so the toy never goes silent.
    """
    name = "gemini_tts"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, api_key: str, model: str, timeout_s: float, fallback: TTSProvider | None):
        if not api_key:
            raise RuntimeError("gemini_tts needs GEMINI_API_KEY in .env")
        self.api_key, self.model, self.timeout_s, self.fallback = api_key, model, timeout_s, fallback

    async def _gemini(self, text: str, cfg: dict) -> bytes:
        style = cfg.get("style") or "Say this in a lively, friendly way"
        body = {
            "contents": [{"parts": [{"text": f"{style}:\n{text}"}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": cfg.get("voice", "Puck")}}},
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            r = await client.post(self.URL.format(model=self.model), json=body,
                                  headers={"x-goog-api-key": self.api_key})
        if r.status_code >= 400:
            raise ProviderError("tts", f"Gemini HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500)
        try:
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
        except (KeyError, IndexError, ValueError):
            raise ProviderError("tts", f"Gemini returned no audio: {r.text[:200]}")
        import base64 as _b64
        pcm = _b64.b64decode(part["data"])
        rate = 24000
        m = re.search(r"rate=(\d+)", part.get("mimeType", ""))
        if m:
            rate = int(m.group(1))
        return await _pcm_to_mp3(pcm, rate, cfg)

    async def synthesize(self, text: str, voice: dict) -> SpeechAudio:
        cfg = (voice or {}).get("gemini_tts")
        if cfg:
            try:
                data = await asyncio.wait_for(self._gemini(speakable(text) or text, cfg), self.timeout_s)
                return SpeechAudio(data=data, mime="audio/mpeg", meta={"voice": cfg.get("voice"), "engine": "gemini"})
            except Exception as e:
                if not self.fallback:
                    raise ProviderError("tts", f"Gemini TTS failed: {e}", retryable=True)
                log.warning("Gemini TTS failed (%s); falling back to %s", getattr(e, "message", e), self.fallback.name)
        if not self.fallback:
            raise ProviderError("tts", "personality has no gemini_tts voice configured")
        return await self.fallback.synthesize(text, voice)


async def _pcm_to_mp3(pcm: bytes, rate: int, cfg: dict) -> bytes:
    """Raw 16-bit mono PCM -> trimmed, loudness-normalized MP3.

    target_f0 (Hz): measure this clip's median pitch and shift pitch+formants so every line lands at the
    character's pitch (Gemini's acted delivery varies a lot line to line). Otherwise kid_shift is a fixed factor.
    """
    shift = float(cfg.get("kid_shift") or 1.0)
    target = cfg.get("target_f0")
    if target:
        import numpy as np
        from backend.ai.audio import median_f0
        f0 = median_f0(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768, rate)
        if f0:
            lo, hi = cfg.get("shift_range", (0.85, 1.4))
            shift = min(hi, max(lo, float(target) / f0))
            log.debug("gemini clip f0 %.0f Hz -> shift %.2f", f0, shift)
    trim = "silenceremove=start_periods=1:start_threshold=-42dB:start_silence=0.03"
    af = f"{trim},areverse,{trim},areverse"
    if shift != 1.0:
        af += f",asetrate={int(rate * shift)},aresample={rate},atempo={max(0.5, min(2.0, 1 / shift)):.4f}"
    af += ",loudnorm=I=-16"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
        "-af", af, "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "-",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(pcm)
    if proc.returncode != 0 or not out:
        raise RuntimeError(err.decode(errors="ignore")[:200])
    return out


class MockTTS(TTSProvider):
    name = "mock"

    async def synthesize(self, text: str, voice: dict) -> SpeechAudio:
        return SpeechAudio(data=make_tone_wav(), mime="audio/wav", meta={"voice": "beep"})


def build_tts(s) -> TTSProvider:
    p = s.tts_provider
    if p == "macos_say":
        return MacSayTTS(s.tts_timeout_s)
    if p == "openai_compat":
        return OpenAICompatTTS(s.tts_base_url, s.tts_api_key, s.tts_model, s.tts_timeout_s)
    if p == "edge_tts":
        fallback = MacSayTTS(s.tts_timeout_s) if shutil.which("say") else None
        return EdgeTTS(min(s.tts_timeout_s, 15.0), fallback)
    if p == "gemini_tts":
        say = MacSayTTS(s.tts_timeout_s) if shutil.which("say") else None
        return GeminiTTS(s.gemini_api_key, s.gemini_tts_model, min(s.tts_timeout_s, 20.0),
                         EdgeTTS(min(s.tts_timeout_s, 15.0), say))
    if p == "mock":
        return MockTTS()
    raise ValueError(f"Unknown TTS_PROVIDER '{p}' (use macos_say | edge_tts | gemini_tts | openai_compat | mock)")
