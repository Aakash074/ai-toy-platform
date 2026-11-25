"""The conversation pipeline:  audio → STT → safety → LLM → safety → TTS → audio.

One turn per device at a time (per-device lock). Every stage is timed, logged, and
failure-tolerant: a failing provider produces a friendly spoken fallback rather than a
crash, and the device LED goes to ERROR briefly so the failure is visible on hardware.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.ai.audio import wav_duration
from backend.ai.base import ChatMessage, LLMProvider, LLMResult, ProviderError, STTProvider, TTSProvider
from backend.devices.registry import DeviceRegistry
from backend.personalities.registry import PersonalityRegistry
from backend.safety import guard
from backend.services import child_profile
from backend.services.events import DeviceHub, EventBus
from backend.services.usage import UsageService
from backend.storage.db import Database, dumps, now

log = logging.getLogger("pipeline")

LLM_FAILURE_REPLY = "Oops, my thinking cap slipped off! Can you say that again?"
NOT_HEARD_REPLY = "Hmm, I didn't quite hear you. Can you say that again a little louder?"
LLM_FAILURE_REPLIES = {
    "en": LLM_FAILURE_REPLY,
    "hi": "अरे, मेरी सोचने वाली टोपी गिर गई! क्या तुम फिर से बोलोगे?",
    "te": "అయ్యో, నా ఆలోచనల టోపీ జారిపోయింది! మళ్ళీ చెబుతావా?",
}
NOT_HEARD_REPLIES = {
    "en": NOT_HEARD_REPLY,
    "hi": "हम्म, मैंने ठीक से नहीं सुना। क्या तुम थोड़ा ज़ोर से फिर से बोलोगे?",
    "te": "హ్మ్, నాకు సరిగ్గా వినిపించలేదు. కొంచెం గట్టిగా మళ్ళీ చెబుతావా?",
}


# End of a sentence followed by whitespace (Latin + Devanagari danda); closing quotes/brackets stay attached.
_SENTENCE_END = re.compile(r"[.!?।॥]+[\"'”’)\]]*\s+")


class TurnBusy(Exception):
    pass


@dataclass
class TurnResult:
    device_id: str
    personality_id: str
    transcript: str
    reply: str
    audio_b64: str | None
    audio_mime: str
    flags: list[str] = field(default_factory=list)
    timings_ms: dict = field(default_factory=dict)
    error: str | None = None
    conversation_id: int | None = None
    language: str = "en"  # personality language, e.g. for the browser speechSynthesis fallback

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class ConversationService:
    def __init__(self, *, db: Database, devices: DeviceRegistry, personalities: PersonalityRegistry,
                 usage: UsageService, bus: EventBus, hub: DeviceHub, settings,
                 stt: STTProvider, llm: LLMProvider, tts: TTSProvider, voice_lines=None):
        self.db, self.devices, self.personalities, self.usage = db, devices, personalities, usage
        self.voice_lines = voice_lines
        self.bus, self.hub, self.settings = bus, hub, settings
        self.stt, self.llm, self.tts = stt, llm, tts
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------- device state helpers ----------
    async def set_device_state(self, device_id: str, state: str, **extra) -> None:
        self.devices.set_state(device_id, state)
        await self.hub.send(device_id, {"type": "set_state", "state": state})
        self.bus.publish("device_state", device_id=device_id, state=state, **extra)

    async def _flash_error_then(self, device_id: str, state: str) -> None:
        await self.set_device_state(device_id, "ERROR")
        await asyncio.sleep(1.5)
        if self.devices.live.get(device_id) and self.devices.live[device_id].state == "ERROR":
            await self.set_device_state(device_id, state)

    def fixed_line(self, personality, key: str, defaults: dict) -> str:
        """A personality's pre-made line (recorded with the expressive voice) or the generic localized one."""
        if self.voice_lines:
            line = self.voice_lines.line(personality.id, key)
            if line:
                return line
        return guard.localized(defaults, personality.language)

    def story_prompt(self, personality) -> str:
        stories = self.voice_lines.stories(personality.id) if self.voice_lines else {}
        if not stories:
            return ""
        items = "; ".join(f"{k} = {v['title']}" for k, v in stories.items())
        return ("\nRECORDED STORIES: when the child asks for a story, you may pick one of these pre-recorded "
                f"stories: {items}. To play it, write ONE short excited intro sentence (do not tell the story "
                "yourself) and then the hidden note <<story=ID>>. Don't repeat a story you already told today "
                "unless asked; you can also make up a new tiny story instead.")

    # ---------- history ----------
    def history(self, device_id: str, limit: int | None = None) -> list[dict]:
        limit = limit or 50
        rows = self.db.query(
            "SELECT * FROM conversations WHERE device_id = ? ORDER BY id DESC LIMIT ?", (device_id, limit))
        for r in rows:
            r["safety_flags"] = json.loads(r["safety_flags"] or "[]")
            r["latency_ms"] = json.loads(r["latency_ms"] or "{}")
        return list(reversed(rows))

    def _llm_context(self, device_id: str, personality_id: str) -> list[ChatMessage]:
        n = self.settings.llm_history_turns
        if n <= 0:
            return []
        rows = self.db.query(
            "SELECT user_text, reply_text FROM conversations WHERE device_id = ? AND personality_id = ? "
            "ORDER BY id DESC LIMIT ?", (device_id, personality_id, n))
        msgs: list[ChatMessage] = []
        for r in reversed(rows):
            msgs += [ChatMessage("user", r["user_text"]), ChatMessage("assistant", r["reply_text"])]
        return msgs

    # ---------- the pipeline ----------
    async def run_turn(self, device_id: str, wav_bytes: bytes, *, text_override: str | None = None,
                       emit=None) -> TurnResult:
        """emit: optional async callback. When given, the reply is streamed: each sentence is voiced as soon
        as the LLM finishes writing it and passed to emit({"type": "audio", ...}) in order, so playback
        starts long before the whole reply exists. The returned TurnResult then has audio_b64=None."""
        lock = self._locks.setdefault(device_id, asyncio.Lock())
        if lock.locked():
            raise TurnBusy(f"device {device_id} is already processing a turn")
        async with lock:
            return await self._run_turn(device_id, wav_bytes, text_override, emit)

    async def _run_turn(self, device_id: str, wav_bytes: bytes, text_override: str | None, emit=None) -> TurnResult:
        t0 = time.perf_counter()
        timings: dict[str, int] = {}
        flags: list[str] = []
        error: str | None = None

        device = self.devices.get(device_id)
        if not device:
            raise KeyError(device_id)
        personality = self.personalities.get(device["personality_id"]) or self.personalities.get(self.personalities.default_id())
        audio_s = wav_duration(wav_bytes) if wav_bytes else 0.0
        level = ""
        try:  # loudness diagnostics only; nothing is stored
            from backend.ai.audio import wav_to_float32
            import numpy as _np
            _x, _ = wav_to_float32(wav_bytes, 16000)
            level = f" rms={float(_np.sqrt(_np.mean(_x ** 2))):.4f} peak={float(_np.max(_np.abs(_x))):.3f}"
        except Exception:
            pass
        log.info("[audio] device=%s received %.1fs of audio (%d bytes)%s", device_id, audio_s, len(wav_bytes or b""), level)
        self.devices.touch(device_id)

        if self.settings.save_raw_audio and wav_bytes:
            p = Path(self.settings.db_path).parent / "audio" / f"{device_id}-{int(time.time())}.wav"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(wav_bytes)

        await self.set_device_state(device_id, "THINKING", stage="stt")

        # 1) Parental limits & subscription quota (checked before spending any AI usage)
        minutes, turns = self.usage.today_minutes_and_turns(device_id)
        limit = guard.check_limits(device, minutes, turns)
        quota = self.usage.check_quota(device.get("account_id"), self.settings.enforce_quotas)
        if not limit.allowed or not quota.allowed:
            reason = limit.reason or quota.reason
            reply = limit.reply or "Our play time is used up for now. Please ask a grown-up to check the toy settings."
            log.info("[safety] device=%s turn blocked: %s", device_id, reason)
            return await self._finish(device, personality, "", reply, [f"limit:{reason}"], timings, t0, audio_s, None, store=False)

        # 2) Speech-to-text
        if text_override is not None:
            transcript = text_override.strip()
        else:
            try:  # one-time model load/download is not counted against the STT timeout
                await self.stt.warmup(personality.language)
            except Exception as e:
                log.error("[ai] STT warmup failed: %s", e)
            ts = time.perf_counter()
            try:
                tr = await asyncio.wait_for(self.stt.transcribe(wav_bytes, personality.language),
                                            self.settings.stt_timeout_s + 5)
                transcript = tr.text
            except (ProviderError, asyncio.TimeoutError) as e:
                log.error("[ai] STT failed for %s: %s", device_id, e)
                transcript, error = "", f"STT error: {getattr(e, 'message', 'timeout')}"
            timings["stt"] = int((time.perf_counter() - ts) * 1000)
            self.usage.record(device_id, device.get("account_id"), "stt_seconds", audio_s, self.stt.name)
        log.info("[ai] STT (%s) %dms: %r", self.stt.name, timings.get("stt", 0), transcript)
        self.bus.publish("transcript", device_id=device_id, text=transcript)
        if emit:
            await emit({"type": "transcript", "text": transcript})

        if not transcript or len(transcript.strip(" .,!?")) < 2:
            reply = self.fixed_line(personality, "not_heard", NOT_HEARD_REPLIES)
            return await self._finish(device, personality, transcript, reply, ["not_heard"], timings, t0,
                                      audio_s, error, store=False)

        # 3) Input safety
        inp = guard.check_input(transcript)
        flags += inp.flags
        if inp.flags:
            log.info("[safety] input flags for %s: %s", device_id, inp.flags)
        if inp.redirect_reply:
            return await self._finish(device, personality, inp.text, inp.redirect_reply, flags, timings, t0, audio_s, None)

        # 4) LLM (one retry for transient failures)
        self.bus.publish("device_state", device_id=device_id, state="THINKING", stage="llm")
        memory = child_profile.load_memory(device)
        system = (guard.build_system_prompt(personality) + "\n\n" + child_profile.prompt_section(device, memory)
                  + self.story_prompt(personality))
        messages = self._llm_context(device_id, personality.id) + [ChatMessage("user", inp.text)]
        if emit:
            return await self._streamed_reply(device, personality, inp.text, system, messages, memory, emit,
                                              flags, timings, t0, audio_s)
        tl = time.perf_counter()
        reply_raw = ""
        for attempt in (1, 2):
            try:
                res = await asyncio.wait_for(
                    self.llm.chat(system, messages, personality.response_length.max_tokens),
                    self.settings.llm_timeout_s + 5)
                reply_raw = res.text
                if res.finish_reason == "refusal":
                    flags.append("llm_refusal")
                self.usage.record(device_id, device.get("account_id"), "llm_input_tokens", res.input_tokens, self.llm.name)
                self.usage.record(device_id, device.get("account_id"), "llm_output_tokens", res.output_tokens, self.llm.name)
                break
            except (ProviderError, asyncio.TimeoutError) as e:
                retryable = getattr(e, "retryable", True)
                log.error("[ai] LLM attempt %d failed: %s", attempt, e)
                if attempt == 1 and retryable:
                    await asyncio.sleep(0.5)
                    continue
                error = f"LLM error: {getattr(e, 'message', 'timeout')}"
                reply_raw = self.fixed_line(personality, "llm_failure", LLM_FAILURE_REPLIES)
                flags.append("llm_failed")
                break
        timings["llm"] = int((time.perf_counter() - tl) * 1000)
        log.info("[ai] LLM (%s/%s) %dms: %r", self.llm.name, getattr(self.llm, "model", ""), timings["llm"], reply_raw)

        # 4b) Memory notes (hidden tags) -> child profile; never spoken or stored in the transcript
        reply_raw, found = child_profile.parse_tags(reply_raw)
        story_id = (found.get("story") or [None])[0]
        story = self.voice_lines.stories(personality.id).get(story_id) if (story_id and self.voice_lines) else None
        if "llm_failed" not in flags:
            memory = child_profile.apply_updates(memory, found)
            memory["turns"] = int(memory.get("turns", 0)) + 1
            self.devices.update(device_id, {"child_memory": json.dumps(memory, ensure_ascii=False)})
            if found:
                log.info("[memory] %s learned %s", device_id, {k: v for k, v in found.items()})

        # 5) Output safety
        out = guard.check_output(reply_raw, personality.response_length.max_sentences, personality.language)
        flags += out.flags
        if out.flags:
            log.info("[safety] output flags for %s: %s", device_id, out.flags)
        reply = out.text
        if story and not [f for f in out.flags if f != "trimmed"]:
            flags.append(f"story:{story_id}")
            reply = f"{reply} {story['text']}".strip()

        return await self._finish(device, personality, inp.text, reply, flags, timings, t0, audio_s, error)

    async def _streamed_reply(self, device, personality, user_text, system, messages, memory, emit,
                              flags, timings, t0, audio_s) -> TurnResult:
        """LLM stream -> sentences -> (output safety per sentence) -> parallel TTS -> emit in order."""
        device_id = device["id"]
        max_s = personality.response_length.max_sentences
        spoken: list[str] = []
        queue: asyncio.Queue = asyncio.Queue()
        state = {"stop": False, "bytes": 0, "mime": "audio/wav", "first": None, "tts_failed": False}
        error: str | None = None

        async def tts_one(text: str):
            return await asyncio.wait_for(self.tts.synthesize(text, personality.voice), self.settings.tts_timeout_s + 5)

        async def sender():
            while (item := await queue.get()) is not None:
                seq, text, task = item
                b64, mime = None, "audio/wav"
                try:
                    speech = await task
                    b64, mime = base64.b64encode(speech.data).decode(), speech.mime
                    state["bytes"] += len(speech.data)
                    state["mime"] = mime
                except Exception as e:  # browser falls back to its own voice for this sentence
                    log.error("[ai] TTS failed for sentence %d: %s", seq, e)
                    state["tts_failed"] = True
                if state["first"] is None:
                    state["first"] = int((time.perf_counter() - t0) * 1000)
                    await self.set_device_state(device_id, "SPEAKING")
                await emit({"type": "audio", "seq": seq, "text": text, "audio_b64": b64, "audio_mime": mime,
                            "language": personality.language})

        def speak(sentence: str) -> None:
            if state["stop"] or not sentence.strip():
                return
            if len(spoken) >= max_s:
                state["stop"] = True
                if "trimmed" not in flags:
                    flags.append("trimmed")
                return
            out = guard.check_output(sentence, 99, personality.language)
            bad = [f for f in out.flags if f not in ("trimmed", "empty_reply")]
            if "empty_reply" in out.flags:
                return
            if bad:  # unsafe content: say the safe fallback instead and stop the reply here
                flags.extend(f for f in bad if f not in flags)
                log.info("[safety] streamed output flags for %s: %s", device_id, bad)
                state["stop"] = True
            spoken.append(out.text)
            queue.put_nowait((len(spoken) - 1, out.text, asyncio.create_task(tts_one(out.text))))

        def drain(buf: str, final: bool) -> str:
            """Speak every complete sentence in buf; hidden memory tags (<<...>>) are never spoken."""
            cut = buf.find("<<")
            region = buf if cut < 0 else buf[:cut]
            pos = 0
            for m in _SENTENCE_END.finditer(region):
                speak(region[pos:m.end()].strip())
                pos = m.end()
            if final:
                rest, _ = child_profile.parse_tags(buf[pos:])
                speak(rest.strip())
                return ""
            return buf[pos:]

        send_task = asyncio.create_task(sender())
        tl = time.perf_counter()
        raw, buf, result = "", "", None
        for attempt in (1, 2):
            try:
                if hasattr(self.llm, "stream_chat"):
                    agen = self.llm.stream_chat(system, messages, personality.response_length.max_tokens)
                    try:
                        async with asyncio.timeout(self.settings.llm_timeout_s + 5):
                            async for part in agen:
                                if isinstance(part, LLMResult):
                                    result = part
                                    continue
                                if timings.get("llm_first") is None:
                                    timings["llm_first"] = int((time.perf_counter() - tl) * 1000)
                                raw += part
                                buf = drain(buf + part, final=False)
                                if state["stop"]:
                                    break  # enough sentences / unsafe: stop paying for more tokens
                    finally:
                        await agen.aclose()
                else:  # provider without streaming: whole reply at once, still voiced sentence by sentence
                    result = await asyncio.wait_for(self.llm.chat(system, messages, personality.response_length.max_tokens),
                                                    self.settings.llm_timeout_s + 5)
                    raw = buf = result.text
                break
            except (ProviderError, TimeoutError, asyncio.TimeoutError) as e:
                log.error("[ai] LLM stream attempt %d failed: %s", attempt, e)
                if attempt == 1 and getattr(e, "retryable", True) and not spoken:
                    raw, buf = "", ""
                    await asyncio.sleep(0.5)
                    continue
                error = f"LLM error: {getattr(e, 'message', 'timeout')}"
                if not spoken:
                    flags.append("llm_failed")
                    raw = buf = self.fixed_line(personality, "llm_failure", LLM_FAILURE_REPLIES)
                break
        if result and result.finish_reason == "refusal" and not spoken:
            flags.append("llm_refusal")
            raw = buf = self.fixed_line(personality, "llm_failure", LLM_FAILURE_REPLIES)
        drain(buf, final=True)
        if not spoken:  # model returned nothing usable
            speak(self.fixed_line(personality, "not_heard", NOT_HEARD_REPLIES))
        # a chosen pre-recorded story is played whole after the intro sentence
        story_id = (child_profile.parse_tags(raw)[1].get("story") or [None])[0]
        story = (self.voice_lines.stories(personality.id).get(story_id) if (story_id and self.voice_lines) else None)
        if story and not state["stop"]:
            flags.append(f"story:{story_id}")
            spoken.append(story["text"])
            queue.put_nowait((len(spoken) - 1, story["text"], asyncio.create_task(tts_one(story["text"]))))
        timings["llm"] = int((time.perf_counter() - tl) * 1000)
        if result:
            self.usage.record(device_id, device.get("account_id"), "llm_input_tokens", result.input_tokens, self.llm.name)
            self.usage.record(device_id, device.get("account_id"), "llm_output_tokens", result.output_tokens, self.llm.name)
        log.info("[ai] LLM stream (%s/%s) first token %sms, done %dms: %r", self.llm.name,
                 getattr(self.llm, "model", ""), timings.get("llm_first"), timings["llm"], raw)

        # memory notes from the full raw reply
        _, found = child_profile.parse_tags(raw)
        if "llm_failed" not in flags:
            memory = child_profile.apply_updates(memory, found)
            memory["turns"] = int(memory.get("turns", 0)) + 1
            self.devices.update(device_id, {"child_memory": json.dumps(memory, ensure_ascii=False)})
            if found:
                log.info("[memory] %s learned %s", device_id, dict(found))

        queue.put_nowait(None)
        await send_task
        if state["tts_failed"]:
            flags.append("tts_failed")
        timings["first_audio"] = state["first"] or 0
        reply = " ".join(spoken)
        self.usage.record(device_id, device.get("account_id"), "tts_chars", len(reply), self.tts.name)
        return await self._finish(device, personality, user_text, reply, flags, timings, t0, audio_s, error,
                                  prespoken=(state["bytes"], state["mime"]))

    async def _finish(self, device, personality, user_text, reply, flags, timings, t0, audio_s, error,
                      store: bool = True, prespoken: tuple | None = None) -> TurnResult:
        device_id = device["id"]
        audio_b64, mime = None, "audio/wav"
        if prespoken is None:
            # 6) Text-to-speech
            self.bus.publish("device_state", device_id=device_id, state="THINKING", stage="tts")
            tt = time.perf_counter()
            try:
                speech = await asyncio.wait_for(self.tts.synthesize(reply, personality.voice), self.settings.tts_timeout_s + 5)
                audio_b64, mime = base64.b64encode(speech.data).decode(), speech.mime
                self.usage.record(device_id, device.get("account_id"), "tts_chars", len(reply), self.tts.name)
            except (ProviderError, asyncio.TimeoutError) as e:
                log.error("[ai] TTS failed: %s (dashboard will fall back to browser speech)", e)
                error = error or f"TTS error: {getattr(e, 'message', 'timeout')}"
                flags = flags + ["tts_failed"]
            timings["tts"] = int((time.perf_counter() - tt) * 1000)
        timings["total"] = int((time.perf_counter() - t0) * 1000)

        # 7) Persist (text only — raw audio is never stored unless SAVE_RAW_AUDIO=true) + usage
        conv_id = None
        if store and device.get("store_transcripts", 1) and user_text:
            cur = self.db.execute(
                "INSERT INTO conversations (device_id, personality_id, user_text, reply_text, safety_flags, audio_seconds, latency_ms, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (device_id, personality.id, user_text, reply, dumps(flags), audio_s, dumps(timings), now()))
            conv_id = cur.lastrowid
        if user_text:
            self.usage.record(device_id, device.get("account_id"), "turn", 1)
            self.usage.record(device_id, device.get("account_id"), "conversation_seconds", audio_s)

        result = TurnResult(device_id=device_id, personality_id=personality.id, transcript=user_text, reply=reply,
                            audio_b64=audio_b64, audio_mime=mime, flags=flags, timings_ms=timings, error=error,
                            conversation_id=conv_id, language=personality.language)
        log.info("[pipeline] device=%s done in %dms timings=%s flags=%s", device_id, timings["total"], timings, flags)
        self.bus.publish("turn", device_id=device_id, conversation_id=conv_id, transcript=user_text, reply=reply,
                         personality_id=personality.id, flags=flags, timings_ms=timings, error=error)
        if error:
            asyncio.create_task(self._flash_error_then(device_id, "SPEAKING"))
        elif prespoken is None:
            await self.set_device_state(device_id, "SPEAKING")
        # Safety net: if no client reports playback_finished (e.g. tab closed, API-only client),
        # return the toy to CONNECTED once the reply has had time to play.
        if prespoken is not None:
            n_bytes, mime_s = prespoken
            bytes_per_s = 8000 if "mpeg" in mime_s else 44100  # 64 kbps MP3 vs 22 kHz 16-bit WAV
            speak_s = (n_bytes / bytes_per_s if n_bytes else len(reply) / 12) + 3
        else:
            bytes_per_s = 6000 if "mpeg" in mime else 44100  # 48 kbps MP3 vs 22 kHz 16-bit WAV
            speak_s = (len(base64.b64decode(audio_b64)) / bytes_per_s if audio_b64 else len(reply) / 12) + 3
        asyncio.create_task(self._revert_if_still(device_id, "SPEAKING", speak_s))
        return result

    async def _revert_if_still(self, device_id: str, state: str, after_s: float) -> None:
        await asyncio.sleep(after_s)
        ls = self.devices.live.get(device_id)
        if ls and ls.state == state:
            await self.set_device_state(device_id, "CONNECTED")
