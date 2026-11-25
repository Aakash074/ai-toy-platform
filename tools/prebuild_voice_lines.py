"""Record the pre-made lines (config/voice_lines.yaml) once with the character's recording voice:
a consented IndicF5 voice clone (personality voice.clone; runs locally in .voiceclone-venv), or Gemini.

Free-tier Gemini keys allow only a few requests per minute, so this goes slowly and politely:
one request at a time, a pause between them, and patient retries on "429 too many requests".
It is resumable: lines already recorded (same text + same voice settings) are skipped.

    .venv/bin/python tools/prebuild_voice_lines.py                 # all personalities in voice_lines.yaml
    .venv/bin/python tools/prebuild_voice_lines.py bheem_hi        # just one
"""
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLONE_PY = ROOT / ".voiceclone-venv" / "bin" / "python"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import Settings  # noqa: E402
from backend.ai.tts.providers import GeminiTTS  # noqa: E402
from backend.ai.tts.voice_cache import VoiceCache, VoiceLines  # noqa: E402
from backend.personalities.registry import PersonalityRegistry  # noqa: E402


def to_mp3(wav: Path) -> bytes:
    trim = "silenceremove=start_periods=1:start_threshold=-42dB:start_silence=0.03"
    return subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(wav), "-af", f"{trim},areverse,{trim},areverse,loudnorm=I=-16",
         "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "-"], capture_output=True, check=True).stdout


def record_with_clone(todo, cache) -> None:
    """All lines for one reference voice in a single IndicF5 run (model loads once)."""
    if not CLONE_PY.exists():
        sys.exit("voice clone env missing: create .voiceclone-venv (see README) first")
    groups: dict[tuple, list] = {}
    for pid, voice, text in todo:
        c = voice["clone"]
        groups.setdefault((c["ref"], c["ref_text"]), []).append((pid, voice, text))
    for (ref, ref_text), items in groups.items():
        with tempfile.TemporaryDirectory() as td:
            jobs = [{"text": t, "out": str(Path(td) / f"{i}.wav")} for i, (_, _, t) in enumerate(items)]
            spec = Path(td) / "jobs.json"
            spec.write_text(json.dumps({"ref": str(ROOT / ref), "ref_text": ref_text, "nfe": 32, "jobs": jobs},
                                       ensure_ascii=False), encoding="utf-8")
            proc = subprocess.Popen([str(CLONE_PY), str(ROOT / "tools" / "indicf5_render.py"), str(spec)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=ROOT)
            by_out = {j["out"]: it for j, it in zip(jobs, items)}
            done = 0
            for line in proc.stdout:
                line = line.strip()
                if line == "READY":
                    print(f"clone model ready; recording {len(items)} line(s)", flush=True)
                elif line.startswith("OK "):
                    _, out, secs = line.split(" ", 2)
                    pid, voice, text = by_out[out]
                    cache.put(text, voice, to_mp3(Path(out)))
                    done += 1
                    print(f"[{done}/{len(items)}] {pid}: recorded {len(text)} chars in {secs}s", flush=True)
                elif line.startswith("FAIL "):
                    print(line, flush=True)
            proc.wait()


async def main(only: list[str]):
    s = Settings()
    cache = VoiceCache(s.db_path.parent / "voice_cache")
    lines = VoiceLines(s.personalities_path.parent / "voice_lines.yaml")
    pers = PersonalityRegistry(s.personalities_path)
    todo, clone_todo = [], []
    for pid in (only or list(lines.data)):
        p = pers.get(pid)
        if p and (p.voice or {}).get("clone"):
            clone_todo += [(pid, p.voice, t) for t in lines.all_texts(pid) if cache.get(t, p.voice) is None]
            continue
        if not p or not (p.voice or {}).get("gemini_tts"):
            print(f"skip {pid}: no clone or gemini_tts voice in personalities.yaml")
            continue
        for text in lines.all_texts(pid):
            if cache.get(text, p.voice) is None:
                todo.append((pid, p.voice, text))
    if clone_todo:
        print(f"{len(clone_todo)} line(s) to record with the voice clone", flush=True)
        record_with_clone(clone_todo, cache)
    if not todo:
        print("done", flush=True)
        return
    if not s.gemini_api_key:
        sys.exit("GEMINI_API_KEY is not set in .env")
    gem = GeminiTTS(s.gemini_api_key, s.gemini_tts_model, 60, fallback=None)
    print(f"{len(todo)} line(s) to record with Gemini", flush=True)
    for n, (pid, voice, text) in enumerate(todo, 1):
        wait = 20
        while True:
            t0 = time.time()
            try:
                sp = await gem.synthesize(text, voice)
                cache.put(text, voice, sp.data)
                print(f"[{n}/{len(todo)}] {pid}: recorded {len(text)} chars in {time.time() - t0:.1f}s", flush=True)
                break
            except Exception as e:
                msg = str(e)
                if "429" in msg or "quota" in msg.lower():
                    print(f"[{n}/{len(todo)}] rate limited, waiting {wait}s", flush=True)
                    await asyncio.sleep(wait)
                    wait = min(wait * 2, 300)
                    continue
                print(f"[{n}/{len(todo)}] {pid}: FAILED ({msg[:160]})", flush=True)
                break
        await asyncio.sleep(12)  # stay under free-tier per-minute limits
    print("done", flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
