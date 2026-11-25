"""Device management REST API (dashboard + ESP32 REST fallback)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from backend.api.deps import ctx, require_device
from backend.context import AppContext

log = logging.getLogger("api.devices")
router = APIRouter(prefix="/api", tags=["devices"])


class VirtualDeviceIn(BaseModel):
    device_id: str = Field(..., min_length=3, max_length=64)
    name: str | None = None
    personality_id: str | None = None


class RegisterIn(BaseModel):
    device_id: str
    name: str | None = None
    firmware_version: str | None = None
    hardware: str | None = None
    mac: str | None = None


class DevicePatch(BaseModel):
    name: str | None = None
    personality_id: str | None = None
    account_id: str | None = None
    daily_minutes_limit: float | None = Field(None, ge=0, le=1440)
    daily_turns_limit: int | None = Field(None, ge=0, le=10000)
    quiet_hours_start: str | None = Field(None, pattern=r"^(\d{2}:\d{2})?$")
    quiet_hours_end: str | None = Field(None, pattern=r"^(\d{2}:\d{2})?$")
    store_transcripts: bool | None = None
    # child profile (first name / nickname only)
    child_name: str | None = Field(None, max_length=20)
    child_age: int | None = Field(None, ge=0, le=12)  # 0 = unknown
    child_interests: str | None = Field(None, max_length=200)
    reset_child_memory: bool | None = None  # true = forget everything the toy learned


class CommandIn(BaseModel):
    type: str = Field(..., pattern=r"^(set_state|led|ping|reboot|ota_check|identify)$")
    state: str | None = None
    pattern: str | None = None


def check_device_token(c: AppContext, token: str | None) -> None:
    if c.settings.device_token and token != c.settings.device_token:
        raise HTTPException(401, "invalid device token")


@router.get("/devices")
def list_devices(c: AppContext = Depends(ctx)):
    return [c.devices.view(d) | {"usage": c.usage.device_stats(d["id"])["today"]} for d in c.devices.list()]


@router.post("/devices", status_code=201)
def create_virtual_device(body: VirtualDeviceIn, c: AppContext = Depends(ctx)):
    if c.devices.get(body.device_id):
        raise HTTPException(409, "device already exists")
    pid = body.personality_id or c.personalities.default_id()
    if not c.personalities.get(pid):
        raise HTTPException(400, f"unknown personality '{pid}'")
    try:
        d = c.devices.register(body.device_id, name=body.name, is_virtual=True, personality_id=pid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    c.bus.publish("devices_changed")
    return c.devices.view(d)


@router.get("/devices/{device_id}")
def get_device(device_id: str, c: AppContext = Depends(ctx)):
    d = require_device(c, device_id)
    return c.devices.view(d) | {"usage": c.usage.device_stats(device_id), "ws_connected": c.hub.is_connected(device_id)}


@router.patch("/devices/{device_id}")
async def update_device(device_id: str, body: DevicePatch, c: AppContext = Depends(ctx)):
    require_device(c, device_id)
    fields = body.model_dump(exclude_unset=True)
    if "personality_id" in fields and not c.personalities.get(fields["personality_id"]):
        raise HTTPException(400, f"unknown personality '{fields['personality_id']}'")
    if "store_transcripts" in fields:
        fields["store_transcripts"] = int(fields["store_transcripts"])
    for k in ("quiet_hours_start", "quiet_hours_end", "child_name", "child_interests"):
        if fields.get(k) == "":
            fields[k] = None
    if fields.get("child_age") == 0:
        fields["child_age"] = None
    if fields.pop("reset_child_memory", None):
        fields["child_memory"] = "{}"
        log.info("[device] %s child memory erased by parent", device_id)
    d = c.devices.update(device_id, fields)
    if "personality_id" in fields:
        log.info("[device] %s personality -> %s", device_id, fields["personality_id"])
        await c.hub.send(device_id, {"type": "personality", "id": fields["personality_id"],
                                     "name": c.personalities.get(fields["personality_id"]).name})
    c.bus.publish("devices_changed", device_id=device_id)
    return c.devices.view(d)


@router.delete("/devices/{device_id}")
def delete_device(device_id: str, c: AppContext = Depends(ctx)):
    require_device(c, device_id)
    c.devices.delete(device_id)
    c.bus.publish("devices_changed")
    return {"deleted": device_id}


@router.post("/devices/{device_id}/command")
async def send_command(device_id: str, body: CommandIn, c: AppContext = Depends(ctx)):
    require_device(c, device_id)
    msg = body.model_dump(exclude_none=True)
    if body.type == "set_state" and body.state:
        await c.conversation.set_device_state(device_id, body.state)
        return {"sent": c.hub.is_connected(device_id)}
    ok = await c.hub.send(device_id, msg)
    return {"sent": ok}


@router.get("/devices/{device_id}/usage")
def device_usage(device_id: str, c: AppContext = Depends(ctx)):
    require_device(c, device_id)
    return c.usage.device_stats(device_id)


# ---- conversation history & deletion (child privacy) ----
@router.get("/devices/{device_id}/conversations")
def device_conversations(device_id: str, limit: int = 50, c: AppContext = Depends(ctx)):
    require_device(c, device_id)
    return c.conversation.history(device_id, min(max(limit, 1), 500))


@router.delete("/devices/{device_id}/conversations")
def delete_device_conversations(device_id: str, c: AppContext = Depends(ctx)):
    require_device(c, device_id)
    n = c.db.execute("DELETE FROM conversations WHERE device_id = ?", (device_id,)).rowcount
    log.info("[privacy] deleted %d conversation turns for %s", n, device_id)
    c.bus.publish("history_cleared", device_id=device_id)
    return {"deleted": n}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, c: AppContext = Depends(ctx)):
    n = c.db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,)).rowcount
    if not n:
        raise HTTPException(404, "not found")
    return {"deleted": conversation_id}


# ---- ESP32 REST fallback (the primary channel is the /ws/device WebSocket) ----
@router.post("/devices/register")
def register_device(body: RegisterIn, x_device_token: str | None = Header(None), c: AppContext = Depends(ctx)):
    check_device_token(c, x_device_token)
    try:
        d = c.devices.register(body.device_id, name=body.name, firmware_version=body.firmware_version,
                               hardware=body.hardware, mac=body.mac, personality_id=c.personalities.default_id())
    except ValueError as e:
        raise HTTPException(400, str(e))
    c.bus.publish("devices_changed", device_id=body.device_id)
    return {"device": c.devices.view(d), "ws_path": f"/ws/device/{body.device_id}"}


@router.get("/ota/{device_id}")
def ota_check(device_id: str, version: str = "", c: AppContext = Depends(ctx)):
    """OTA placeholder: the firmware calls this; V1 never offers an update.
    Later: look up a release channel per device/hardware and return a signed binary URL + sha256."""
    require_device(c, device_id)
    return {"update_available": False, "current": version, "latest": version, "url": None, "sha256": None}
