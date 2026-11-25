"""End-to-end voice test against a RUNNING backend, with the configured (real) providers.

Generates speech with macOS `say` (standing in for a child at the mic), sends it exactly like the
browser does (16 kHz mono WAV, multipart), then checks transcript + reply + returned audio.

    .venv/bin/python tools/e2e_voice_test.py                       # default device, Teddy then Teacher
    .venv/bin/python tools/e2e_voice_test.py --play                # also play replies on the speaker
    .venv/bin/python tools/e2e_voice_test.py --device esp32-xxxx
"""
from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def speak_to_wav(text: str, path: Path) -> bytes:
    subprocess.run(["say", "-v", "Samantha", "--data-format=LEI16@16000", "-o", str(path), text], check=True)
    return path.read_bytes()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--device", default="virtual-toy-01")
    ap.add_argument("--play", action="store_true")
    a = ap.parse_args()

    cases = [
        ("teddy", "Hello teddy, can you tell me a short story about a bunny?", ["story", "bunny"]),
        ("teacher", "Can you help me count to five?", ["count"]),
    ]
    ok = True
    with httpx.Client(base_url=a.url, timeout=120) as c, tempfile.TemporaryDirectory() as td:
        print("health:", c.get("/api/health").json())
        for pid, utterance, expect_words in cases:
            r = c.patch(f"/api/devices/{a.device}", json={"personality_id": pid})
            r.raise_for_status()
            wav = speak_to_wav(utterance, Path(td) / f"{pid}.wav")
            t = time.perf_counter()
            r = c.post(f"/api/conversation/{a.device}/turn", files={"audio": ("speech.wav", wav, "audio/wav")})
            dt = time.perf_counter() - t
            if r.status_code != 200:
                print(f"✗ [{pid}] HTTP {r.status_code}: {r.text}")
                ok = False
                continue
            b = r.json()
            heard = b["transcript"].lower()
            stt_ok = all(w in heard for w in expect_words)
            audio = base64.b64decode(b["audio_b64"]) if b["audio_b64"] else b""
            audio_ok = audio[:4] == b"RIFF" and len(audio) > 10_000
            print(f"\n[{pid}] {dt:.1f}s  timings={b['timings_ms']}  flags={b['flags']}  error={b['error']}")
            print(f"  child said : {utterance}")
            print(f"  STT heard  : {b['transcript']}   {'✓' if stt_ok else '✗'}")
            print(f"  toy replied: {b['reply']}")
            print(f"  reply audio: {len(audio)} bytes WAV {'✓' if audio_ok else '✗'}  (personality={b['personality_id']})")
            ok &= stt_ok and audio_ok and b["personality_id"] == pid
            if a.play and audio:
                p = Path(td) / f"reply-{pid}.wav"
                p.write_bytes(audio)
                subprocess.run(["afplay", str(p)])
        c.patch(f"/api/devices/{a.device}", json={"personality_id": "teddy"})
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
