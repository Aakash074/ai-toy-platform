"""WebSocket endpoints.

/ws/dashboard        : laptop console ⇄ backend (live events, recording/playback status)
/ws/device/{id}      : ESP32 ⇄ backend (hello, heartbeat, button events ⇄ state commands)
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("ws")
router = APIRouter()


@router.websocket("/ws/dashboard")
async def dashboard_ws(ws: WebSocket):
    c = ws.app.state.ctx
    await ws.accept()
    q = c.bus.subscribe()
    log.info("dashboard connected (%d open)", len(c.bus._subscribers))
    await ws.send_text(json.dumps({"type": "hello", "devices": [c.devices.view(d) for d in c.devices.list()]}))

    async def pump():
        while True:
            evt = await q.get()
            await ws.send_text(json.dumps(evt, default=str))

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            did = msg.get("device_id")
            if not did or not c.devices.get(did):
                continue
            t = msg.get("type")
            if t == "recording_started":
                log.info("[audio] recording started for %s", did)
                await c.conversation.set_device_state(did, "LISTENING")
            elif t in ("recording_cancelled", "playback_finished"):
                await c.conversation.set_device_state(did, "CONNECTED")
    except (WebSocketDisconnect, json.JSONDecodeError, RuntimeError):
        pass
    finally:
        pump_task.cancel()
        c.bus.unsubscribe(q)
        log.info("dashboard disconnected")


# ------------------------------------------------------------------------------------------------
# /ws/device/{device_id}  — ESP32 ⇄ backend. Protocol (JSON text frames):
#
#   device → server                                   server → device
#   {"type":"hello", firmware, hardware, mac, ip,     {"type":"welcome", personality, heartbeat_interval_s}
#     name, capabilities}            (must be first)  {"type":"set_state", "state":"THINKING"}
#   {"type":"heartbeat", rssi, uptime_s, free_heap,   {"type":"personality", "id", "name"}
#     state}                                          {"type":"identify"} / {"type":"ping"}
#   {"type":"button", "action":"press"|"release"}     {"type":"reboot"} / {"type":"ota_check"}
#   {"type":"state", "state":"ERROR"}                 {"type":"error", "message"}
#   {"type":"log", "level", "message"}
#   {"type":"pong"}
# ------------------------------------------------------------------------------------------------
HEARTBEAT_INTERVAL_S = 10
HELLO_TIMEOUT_S = 10
MAX_FRAME = 4096


@router.websocket("/ws/device/{device_id}")
async def device_ws(ws: WebSocket, device_id: str):
    c = ws.app.state.ctx
    token = ws.query_params.get("token") or ws.headers.get("x-device-token")
    await ws.accept()
    ip = ws.client.host if ws.client else None

    if c.settings.device_token and token != c.settings.device_token:
        log.warning("[device] %s rejected: bad token from %s", device_id, ip)
        await ws.send_text(json.dumps({"type": "error", "message": "invalid device token"}))
        await ws.close(code=4401)
        return

    # 1) handshake
    try:
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT_S))
        if hello.get("type") != "hello":
            raise ValueError("first message must be hello")
        d = c.devices.register(
            device_id, name=hello.get("name"), firmware_version=str(hello.get("firmware", "unknown"))[:32],
            hardware=str(hello.get("hardware", "esp32"))[:64], mac=hello.get("mac"),
            personality_id=c.personalities.default_id())
    except (asyncio.TimeoutError, ValueError, json.JSONDecodeError, WebSocketDisconnect) as e:
        log.warning("[device] %s handshake failed from %s: %s", device_id, ip, e)
        try:
            await ws.close(code=4400)
        except RuntimeError:
            pass
        return

    old = c.hub.attach(device_id, ws)
    if old is not None:  # device rebooted & reconnected before the old TCP connection timed out
        log.info("[device] %s replacing stale connection", device_id)
        try:
            await old.close(code=4409)
        except Exception:
            pass
    c.devices.mark_online(device_id, hello.get("ip") or ip)
    p = c.personalities.get(d["personality_id"])
    await ws.send_text(json.dumps({
        "type": "welcome", "device_id": device_id, "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
        "personality": {"id": p.id, "name": p.name} if p else None,
    }))
    await ws.send_text(json.dumps({"type": "set_state", "state": "CONNECTED"}))
    log.info("[device] %s ONLINE fw=%s hw=%s ip=%s", device_id, d["firmware_version"], d["hardware"], hello.get("ip") or ip)
    c.bus.publish("device_online", device_id=device_id, firmware_version=d["firmware_version"], ip=hello.get("ip") or ip)

    # 2) message loop. A silent connection (no heartbeat) is closed after 3 missed intervals.
    reason = "disconnected"
    try:
        while True:
            raw = await asyncio.wait_for(ws.receive_text(), HEARTBEAT_INTERVAL_S * 3)
            if len(raw) > MAX_FRAME:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("[device] %s sent invalid JSON", device_id)
                continue
            t = msg.get("type")
            if t == "heartbeat":
                ls = c.devices.heartbeat(device_id, msg)
                c.bus.publish("heartbeat", device_id=device_id, rssi=ls.rssi, uptime_s=ls.uptime_s,
                              free_heap=ls.free_heap, state=ls.state)
            elif t == "button":
                action = msg.get("action")
                if action in ("press", "release", "click", "long_press"):
                    log.info("[device] %s button %s", device_id, action)
                    c.devices.touch(device_id)
                    c.bus.publish("button", device_id=device_id, action=action)
            elif t == "state":
                c.devices.set_state(device_id, msg.get("state", ""))
                c.bus.publish("device_state", device_id=device_id, state=msg.get("state"))
            elif t == "log":
                level = msg.get("level", "info")
                log.log(logging.ERROR if level == "error" else logging.INFO, "[device] %s says: %s", device_id, msg.get("message"))
                c.bus.publish("device_log", device_id=device_id, level=level, message=str(msg.get("message"))[:300])
            elif t == "pong":
                c.devices.heartbeat(device_id, {})
    except asyncio.TimeoutError:
        reason = "heartbeat_timeout"
    except WebSocketDisconnect:
        reason = "disconnected"
    except RuntimeError:
        reason = "connection_closed"
    finally:
        if c.hub.detach(device_id, ws):  # only if this socket is still the current one
            c.devices.mark_offline(device_id)
            log.warning("[device] %s OFFLINE (%s)", device_id, reason)
            c.bus.publish("device_offline", device_id=device_id, reason=reason)
        try:
            await ws.close()
        except Exception:
            pass
