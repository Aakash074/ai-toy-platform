"""Conversation endpoints: the laptop console posts recorded WAV audio here."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.ai.audio import AudioFormatError, wav_to_float32
from backend.api.deps import ctx, require_device
from backend.context import AppContext
from backend.services.conversation import TurnBusy

router = APIRouter(prefix="/api/conversation", tags=["conversation"])
log = logging.getLogger("api.conversation")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class TextTurnIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


@router.post("/{device_id}/turn")
async def audio_turn(device_id: str, audio: UploadFile = File(...), c: AppContext = Depends(ctx)):
    require_device(c, device_id)
    data = await audio.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "audio too large")
    if len(data) < 100:
        raise HTTPException(400, "empty audio")
    try:
        wav_to_float32(data)
    except AudioFormatError as e:
        raise HTTPException(400, f"audio must be PCM WAV: {e}")
    try:
        result = await c.conversation.run_turn(device_id, data)
    except TurnBusy as e:
        raise HTTPException(409, str(e))
    return result.to_dict()


@router.post("/{device_id}/text")
async def text_turn(device_id: str, body: TextTurnIn, c: AppContext = Depends(ctx)):
    """Typed input (debugging / accessibility): skips STT, everything else is identical."""
    require_device(c, device_id)
    try:
        result = await c.conversation.run_turn(device_id, b"", text_override=body.text)
    except TurnBusy as e:
        raise HTTPException(409, str(e))
    return result.to_dict()


def _stream_turn(c: AppContext, device_id: str, wav: bytes, text: str | None) -> StreamingResponse:
    """NDJSON stream: {"type":"transcript"} -> {"type":"audio", seq, text, audio_b64}... -> {"type":"done", ...}.
    Each audio event is one sentence, sent as soon as it is voiced, so playback starts early."""
    q: asyncio.Queue = asyncio.Queue()
    sent = {"audio": 0}

    async def emit(ev: dict):
        if ev.get("type") == "audio":
            sent["audio"] += 1
        await q.put(ev)

    async def run():
        try:
            res = await c.conversation.run_turn(device_id, wav, text_override=text, emit=emit)
            d = res.to_dict()
            if d.get("audio_b64") and not sent["audio"]:  # early-exit replies (limits, not heard...) arrive whole
                await q.put({"type": "audio", "seq": 0, "text": d["reply"], "audio_b64": d["audio_b64"],
                             "audio_mime": d["audio_mime"], "language": d["language"]})
            elif not d.get("audio_b64") and not sent["audio"]:
                await q.put({"type": "audio", "seq": 0, "text": d["reply"], "audio_b64": None,
                             "audio_mime": d["audio_mime"], "language": d["language"]})
            d["audio_b64"] = None
            await q.put({"type": "done", **d})
        except TurnBusy as e:
            await q.put({"type": "error", "status": 409, "detail": str(e)})
        except Exception as e:  # never leave the client hanging
            log.exception("streamed turn failed")
            await q.put({"type": "error", "status": 500, "detail": str(e)})
        finally:
            await q.put(None)

    async def body():
        task = asyncio.create_task(run())
        try:
            while (ev := await q.get()) is not None:
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        finally:
            if not task.done():
                await task  # finish the turn (state, history) even if the client went away

    return StreamingResponse(body(), media_type="application/x-ndjson")


@router.post("/{device_id}/turn_stream")
async def audio_turn_stream(device_id: str, audio: UploadFile = File(...), c: AppContext = Depends(ctx)):
    require_device(c, device_id)
    data = await audio.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "audio too large")
    if len(data) < 100:
        raise HTTPException(400, "empty audio")
    try:
        wav_to_float32(data)
    except AudioFormatError as e:
        raise HTTPException(400, f"audio must be PCM WAV: {e}")
    return _stream_turn(c, device_id, data, None)


@router.post("/{device_id}/text_stream")
async def text_turn_stream(device_id: str, body: TextTurnIn, c: AppContext = Depends(ctx)):
    require_device(c, device_id)
    return _stream_turn(c, device_id, b"", body.text)


GREETINGS = {"en": "Hi friend! I'm so happy to see you. Shall we play?",
             "hi": "नमस्ते दोस्त! चलो, साथ में खेलते हैं!", "te": "నమస్తే మిత్రమా! రా, కలిసి ఆడుకుందాం!"}


@router.post("/{device_id}/greet")
async def greet(device_id: str, c: AppContext = Depends(ctx)):
    """Play the personality's greeting (a pre-recorded expressive line when available) — demo opener."""
    import base64

    d = require_device(c, device_id)
    p = c.personalities.get(d["personality_id"]) or c.personalities.get(c.personalities.default_id())
    text = c.conversation.fixed_line(p, "greeting", GREETINGS)
    await c.conversation.set_device_state(device_id, "SPEAKING")
    try:
        speech = await c.conversation.tts.synthesize(text, p.voice)
        audio, mime = base64.b64encode(speech.data).decode(), speech.mime
        engine = (speech.meta or {}).get("engine", c.conversation.tts.name)
    except Exception as e:
        log.error("greeting TTS failed: %s", e)
        audio, mime, engine = None, "audio/wav", "none"
    asyncio.create_task(c.conversation._revert_if_still(device_id, "SPEAKING", 8))
    return {"text": text, "audio_b64": audio, "audio_mime": mime, "language": p.language,
            "engine": engine, "personality_id": p.id}
