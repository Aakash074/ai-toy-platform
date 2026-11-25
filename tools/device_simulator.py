"""ESP32 device simulator — speaks exactly the firmware's WebSocket protocol.

Use it to test the dashboard without hardware, or to run extra "devices" alongside a real ESP32.

  Interactive (press ENTER to press/release the toy button, 'q' + ENTER to quit):
    .venv/bin/python tools/device_simulator.py --id esp32-sim-01

  Scripted (used by the automated tests):
    .venv/bin/python tools/device_simulator.py --id esp32-sim-01 --script "wait:2,press,wait:3,release,wait:5"

Like the firmware, it reconnects with exponential backoff if the backend goes away.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time

import websockets

FIRMWARE = "sim-0.1.0"


class SimDevice:
    def __init__(self, url: str, device_id: str, token: str = ""):
        self.url, self.device_id, self.token = url.rstrip("/"), device_id, token
        self.state = "BOOTING"
        self.ws = None
        self.started = time.time()
        self.connected = asyncio.Event()
        self.hb_interval = 10

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] [{self.device_id}] {msg}", flush=True)

    def set_state(self, s: str) -> None:
        if s != self.state:
            self.log(f"LED → {s}")
        self.state = s

    async def send(self, obj: dict) -> None:
        if self.ws:
            await self.ws.send(json.dumps(obj))

    async def run_forever(self) -> None:
        backoff = 1.0
        while True:
            self.set_state("CONNECTING_WIFI")
            ws_url = f"{self.url}/ws/device/{self.device_id}" + (f"?token={self.token}" if self.token else "")
            try:
                async with websockets.connect(ws_url, open_timeout=5, ping_interval=20) as ws:
                    self.ws = ws
                    await self.send({"type": "hello", "name": f"Simulated ESP32 ({self.device_id})",
                                     "firmware": FIRMWARE, "hardware": "simulator", "mac": "00:00:00:00:00:00",
                                     "ip": "127.0.0.1", "capabilities": {"led": True, "button": True, "ble": False, "audio": False}})
                    backoff = 1.0
                    hb = asyncio.create_task(self._heartbeat())
                    try:
                        async for raw in ws:
                            await self._on_message(json.loads(raw))
                    finally:
                        hb.cancel()
                        self.connected.clear()
                        self.ws = None
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                self.log(f"connection failed: {e.__class__.__name__}: {e}")
            self.set_state("ERROR")
            delay = min(30.0, backoff) * (0.8 + random.random() * 0.4)
            self.log(f"reconnecting in {delay:.1f}s")
            await asyncio.sleep(delay)
            backoff *= 2

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.hb_interval)
            await self.send({"type": "heartbeat", "rssi": -50 - random.randint(0, 15),
                             "uptime_s": int(time.time() - self.started), "free_heap": 180000, "state": self.state})

    async def _on_message(self, m: dict) -> None:
        t = m.get("type")
        if t == "welcome":
            self.hb_interval = m.get("heartbeat_interval_s", 10)
            self.log(f"registered ✓ personality={m.get('personality', {}).get('name')}")
            self.connected.set()
        elif t == "set_state":
            self.set_state(m["state"])
        elif t == "personality":
            self.log(f"personality → {m.get('name')}")
        elif t == "identify":
            self.log("IDENTIFY: blinking LED")
        elif t == "ping":
            await self.send({"type": "pong"})
        elif t == "reboot":
            self.log("reboot requested")
            await self.ws.close()
        elif t == "error":
            self.log(f"server error: {m.get('message')}")

    async def button(self, action: str) -> None:
        await self.connected.wait()
        self.log(f"button {action}")
        await self.send({"type": "button", "action": action})


async def run_script(dev: SimDevice, script: str) -> None:
    await asyncio.wait_for(dev.connected.wait(), 15)
    for step in [s.strip() for s in script.split(",") if s.strip()]:
        if step.startswith("wait:"):
            await asyncio.sleep(float(step[5:]))
        elif step in ("press", "release", "click"):
            await dev.button(step)
        elif step == "drop":  # simulate abrupt Wi-Fi loss
            dev.log("simulating Wi-Fi drop")
            await dev.ws.close()
        else:
            dev.log(f"unknown step {step}")


async def interactive(dev: SimDevice) -> None:
    pressed = False
    loop = asyncio.get_running_loop()
    print("ENTER = press/release button, q = quit", flush=True)
    while True:
        line = (await loop.run_in_executor(None, sys.stdin.readline)).strip().lower()
        if line == "q":
            return
        pressed = not pressed
        await dev.button("press" if pressed else "release")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.getenv("SIM_BACKEND_WS", "ws://localhost:8000"))
    ap.add_argument("--id", default=os.getenv("DEVICE_ID", "esp32-sim-01"))
    ap.add_argument("--token", default=os.getenv("DEVICE_TOKEN", ""))
    ap.add_argument("--script", help="comma list: wait:N, press, release, click, drop")
    a = ap.parse_args()
    dev = SimDevice(a.url, a.id, a.token)
    runner = asyncio.create_task(dev.run_forever())
    try:
        await (run_script(dev, a.script) if a.script else interactive(dev))
    finally:
        runner.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
