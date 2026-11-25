"""Device registry: persistent device records (SQLite) + live connection state (in memory).

Physical ESP32s and virtual devices share the same model. A virtual device is "online"
whenever the dashboard uses it; a physical device is online while its WebSocket is
connected and heartbeats arrive within HEARTBEAT_TIMEOUT_S.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from backend.storage.db import Database, now

log = logging.getLogger("devices")

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{3,64}$")

# Device-side state machine (mirrors firmware/esp32/include/toy_state.h)
STATES = ("BOOTING", "CONNECTING_WIFI", "CONNECTED", "LISTENING", "THINKING", "SPEAKING", "ERROR", "OFFLINE")

EDITABLE_FIELDS = {
    "name", "personality_id", "account_id", "daily_minutes_limit", "daily_turns_limit",
    "quiet_hours_start", "quiet_hours_end", "store_transcripts",
    "child_name", "child_age", "child_interests", "child_memory",
}


@dataclass
class LiveState:
    online: bool = False
    state: str = "OFFLINE"
    connected_at: float | None = None
    last_heartbeat: float | None = None
    rssi: int | None = None
    ip: str | None = None
    uptime_s: int | None = None
    free_heap: int | None = None
    extra: dict = field(default_factory=dict)


class DeviceRegistry:
    def __init__(self, db: Database, heartbeat_timeout_s: int):
        self.db = db
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.live: dict[str, LiveState] = {}

    # ---- persistent records ----
    def get(self, device_id: str) -> dict | None:
        return self.db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,))

    def list(self) -> list[dict]:
        return self.db.query("SELECT * FROM devices ORDER BY is_virtual, created_at")

    def register(self, device_id: str, *, name: str | None = None, firmware_version: str | None = None,
                 hardware: str | None = None, mac: str | None = None, is_virtual: bool = False,
                 personality_id: str = "teddy") -> dict:
        if not DEVICE_ID_RE.match(device_id):
            raise ValueError("device_id must be 3-64 chars of [A-Za-z0-9_-:.]")
        existing = self.get(device_id)
        if existing:
            updates = {k: v for k, v in {"firmware_version": firmware_version, "hardware": hardware, "mac": mac}.items() if v}
            if name and existing["name"] == existing["id"]:
                updates["name"] = name
            if updates:
                sets = ", ".join(f"{k} = :{k}" for k in updates)
                self.db.execute(f"UPDATE devices SET {sets} WHERE id = :id", {**updates, "id": device_id})
            return self.get(device_id)
        self.db.execute(
            "INSERT INTO devices (id, name, firmware_version, hardware, mac, is_virtual, personality_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (device_id, name or device_id, firmware_version or "n/a", hardware or ("virtual" if is_virtual else "unknown"),
             mac, int(is_virtual), personality_id, now()),
        )
        log.info("Registered new %s device %s", "virtual" if is_virtual else "physical", device_id)
        return self.get(device_id)

    def update(self, device_id: str, fields: dict) -> dict | None:
        fields = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
        if fields:
            sets = ", ".join(f"{k} = :{k}" for k in fields)
            self.db.execute(f"UPDATE devices SET {sets} WHERE id = :id", {**fields, "id": device_id})
        return self.get(device_id)

    def delete(self, device_id: str) -> bool:
        self.live.pop(device_id, None)
        return self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,)).rowcount > 0

    def touch(self, device_id: str) -> None:
        self.db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now(), device_id))

    # ---- live connection state ----
    def mark_online(self, device_id: str, ip: str | None = None) -> LiveState:
        ls = self.live.setdefault(device_id, LiveState())
        ls.online, ls.state, ls.connected_at, ls.ip = True, "CONNECTED", time.time(), ip
        ls.last_heartbeat = time.time()
        self.db.execute("UPDATE devices SET last_seen = ?, last_heartbeat = ?, last_ip = ? WHERE id = ?",
                        (now(), now(), ip, device_id))
        return ls

    def mark_offline(self, device_id: str) -> None:
        ls = self.live.setdefault(device_id, LiveState())
        ls.online, ls.state = False, "OFFLINE"

    def heartbeat(self, device_id: str, payload: dict) -> LiveState:
        ls = self.live.setdefault(device_id, LiveState())
        ls.online = True
        ls.last_heartbeat = time.time()
        ls.rssi = payload.get("rssi", ls.rssi)
        ls.uptime_s = payload.get("uptime_s", ls.uptime_s)
        ls.free_heap = payload.get("free_heap", ls.free_heap)
        if payload.get("state") in STATES:
            ls.state = payload["state"]
        self.db.execute("UPDATE devices SET last_seen = ?, last_heartbeat = ?, last_rssi = ? WHERE id = ?",
                        (now(), now(), ls.rssi, device_id))
        return ls

    def set_state(self, device_id: str, state: str) -> None:
        if state in STATES:
            self.live.setdefault(device_id, LiveState()).state = state

    def expire_stale(self) -> list[str]:
        """Mark physical devices offline when heartbeats stop (e.g. power pulled without a TCP close)."""
        expired = []
        cutoff = time.time() - self.heartbeat_timeout_s
        for did, ls in self.live.items():
            if ls.online and ls.last_heartbeat and ls.last_heartbeat < cutoff and not ls.extra.get("virtual"):
                ls.online, ls.state = False, "OFFLINE"
                expired.append(did)
        return expired

    def view(self, device: dict) -> dict:
        """Merged record + live state, as the dashboard sees it."""
        ls = self.live.get(device["id"], LiveState())
        virtual = bool(device["is_virtual"])
        d = dict(device)
        d["meta"] = json.loads(d.get("meta") or "{}")
        try:
            d["child_memory"] = json.loads(d.get("child_memory") or "{}")
        except ValueError:
            d["child_memory"] = {}
        d["is_virtual"] = virtual
        d["store_transcripts"] = bool(d["store_transcripts"])
        d["online"] = True if virtual else ls.online
        d["state"] = (ls.state if ls.state != "OFFLINE" else "CONNECTED") if virtual else ls.state
        d["wifi"] = {"connected": ls.online, "rssi": ls.rssi if ls.rssi is not None else device.get("last_rssi"),
                     "ip": ls.ip or device.get("last_ip")} if not virtual else {"connected": None, "rssi": None, "ip": None}
        d["uptime_s"], d["free_heap"] = ls.uptime_s, ls.free_heap
        d["connected_at"] = ls.connected_at
        return d
