"""Settings, personalities, accounts and health."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.ai.audio import make_tone_wav
from backend.ai.base import ChatMessage
from backend.api.deps import ctx
from backend.config import RUNTIME_EDITABLE
from backend.context import AppContext
from backend.personalities.registry import Personality, ResponseLength
from backend.safety.guard import SAFETY_PREAMBLE

log = logging.getLogger("api.admin")
router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/health")
def health(c: AppContext = Depends(ctx)):
    return {"ok": True, "devices": len(c.devices.list()), "providers": {
        "stt": c.conversation.stt.name, "llm": c.conversation.llm.name, "tts": c.conversation.tts.name}}


@router.get("/settings")
def get_settings(c: AppContext = Depends(ctx)):
    return c.settings.public_view() | {"safety_preamble": SAFETY_PREAMBLE}


@router.patch("/settings")
def patch_settings(body: dict, c: AppContext = Depends(ctx)):
    bad = set(body) - RUNTIME_EDITABLE
    if bad:
        raise HTTPException(400, f"not runtime-editable (set them in .env): {sorted(bad)}")
    old = {k: getattr(c.settings, k) for k in body}

    def cast(cur, v):
        if isinstance(cur, bool):
            return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        return type(cur)(v) if cur is not None else v

    try:
        new = {k: cast(old[k], v) for k, v in body.items()}
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"bad value: {e}")
    for k, v in new.items():
        setattr(c.settings, k, v)
    try:
        c.rebuild_providers()
    except Exception as e:
        for k, v in old.items():
            setattr(c.settings, k, v)
        c.rebuild_providers()
        raise HTTPException(400, f"invalid provider config: {e}")
    log.info("[settings] runtime update: %s", {k: body[k] for k in body})
    return c.settings.public_view()


@router.post("/settings/test")
async def test_providers(c: AppContext = Depends(ctx)):
    """Smoke-test each provider independently so the Settings screen can show what works."""
    out = {}
    conv = c.conversation
    for name, coro in (
        ("llm", lambda: conv.llm.chat("Reply with exactly: OK", [ChatMessage("user", "Say OK")], 16)),
        ("tts", lambda: conv.tts.synthesize("Hello!", {})),
        ("stt", lambda: conv.stt.transcribe(make_tone_wav(0.5))),
    ):
        t = time.perf_counter()
        try:
            r = await coro()
            detail = getattr(r, "text", None)
            out[name] = {"ok": True, "ms": int((time.perf_counter() - t) * 1000), "detail": detail if detail is not None else "audio ok"}
        except Exception as e:
            out[name] = {"ok": False, "ms": int((time.perf_counter() - t) * 1000), "detail": str(e)[:300]}
    return out


# ---- personalities ----
class PersonalityIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    system_prompt: str = Field(..., min_length=10, max_length=4000)
    age_group: str = "3-8"
    language: str = "en"
    response_length: ResponseLength = ResponseLength()
    teaching_style: str = ""
    voice: dict = Field(default_factory=dict)


@router.get("/personalities")
def list_personalities(c: AppContext = Depends(ctx)):
    return [p.model_dump() for p in c.personalities.all()]


@router.put("/personalities/{pid}")
def upsert_personality(pid: str, body: PersonalityIn, c: AppContext = Depends(ctx)):
    if not pid.replace("_", "").replace("-", "").isalnum() or len(pid) > 40:
        raise HTTPException(400, "invalid personality id")
    p = c.personalities.upsert(Personality(id=pid, **body.model_dump()))
    c.bus.publish("personalities_changed")
    return p.model_dump()


@router.post("/personalities/reload")
def reload_personalities(c: AppContext = Depends(ctx)):
    try:
        c.personalities.reload()
    except Exception as e:
        raise HTTPException(400, f"reload failed: {e}")
    c.bus.publish("personalities_changed")
    return [p.id for p in c.personalities.all()]


# ---- parent accounts (subscription architecture; no payments in V1) ----
class AccountIn(BaseModel):
    account_id: str = Field(..., min_length=3, max_length=64)
    name: str
    email: str | None = None
    plan: str = "free"


@router.post("/accounts", status_code=201)
def create_account(body: AccountIn, c: AppContext = Depends(ctx)):
    return c.usage.create_account(body.account_id, body.name, body.email, body.plan)


@router.get("/accounts/{account_id}")
def get_account(account_id: str, c: AppContext = Depends(ctx)):
    acc = c.usage.get_account(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    return acc
