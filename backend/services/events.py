"""Realtime plumbing.

EventBus  : fan-out of events to every open dashboard WebSocket (device online/offline,
            button presses, pipeline status, new conversation turns...).
DeviceHub : the live WebSocket for each physical device, used to push commands
            (set_state / led / ping / reboot / ota) to the ESP32.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("events")


class EventBus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, type_: str, **data: Any) -> None:
        evt = {"type": type_, "ts": time.time(), **data}
        for q in list(self._subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:  # slow/dead dashboard: drop rather than block the pipeline
                log.warning("dashboard queue full; dropping event %s", type_)


class DeviceHub:
    def __init__(self):
        self._sockets: dict[str, WebSocket] = {}

    def attach(self, device_id: str, ws: WebSocket) -> WebSocket | None:
        """Returns the previous socket (if any) so the caller can close it — a rebooted ESP32
        may reconnect before the server notices the old TCP connection died."""
        old = self._sockets.get(device_id)
        self._sockets[device_id] = ws
        return old

    def detach(self, device_id: str, ws: WebSocket) -> bool:
        if self._sockets.get(device_id) is ws:
            del self._sockets[device_id]
            return True
        return False

    def is_connected(self, device_id: str) -> bool:
        return device_id in self._sockets

    async def send(self, device_id: str, message: dict) -> bool:
        ws = self._sockets.get(device_id)
        if not ws:
            return False
        try:
            await ws.send_text(json.dumps(message))
            log.debug("→ %s %s", device_id, message)
            return True
        except Exception as e:
            log.warning("send to %s failed: %s", device_id, e)
            return False
