"""Live voice conversion service (Seed-VC, zero-shot) for the toy's character voice.

Loads the models ONCE and pre-computes the reference voice, then converts each spoken sentence:
    POST /convert?steps=10   body: WAV bytes (any rate)   ->   WAV (22.05 kHz mono 16-bit)
    GET  /health
Runs in Seed-VC's own environment (GPL-3; kept as a separate process/service):

    PYTORCH_ENABLE_MPS_FALLBACK=1 /path/to/seed-vc/.venv/bin/python tools/seedvc_server.py \
        --seedvc /path/to/seed-vc --ref data/voices/bheem_ref.wav --port 8765

The reference clip must be used with the voice owner's / rights holders' permission.
"""
import argparse
import io
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ap = argparse.ArgumentParser()
ap.add_argument("--seedvc", required=True)
ap.add_argument("--ref", required=True)
ap.add_argument("--port", type=int, default=8765)
ap.add_argument("--steps", type=int, default=10)
ap.add_argument("--cfg-rate", type=float, default=0.7)
opts = ap.parse_args()
REF = os.path.abspath(opts.ref)

os.chdir(opts.seedvc)                 # Seed-VC resolves ./checkpoints relative to its folder
sys.path.insert(0, opts.seedvc)

import librosa  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
import torchaudio  # noqa: E402

import inference as sv  # noqa: E402  (Seed-VC's module)

t0 = time.time()
model, semantic_fn, f0_fn, vocoder_fn, campplus_model, mel_fn, mel_fn_args = sv.load_models(
    argparse.Namespace(f0_condition=False, fp16=False, checkpoint=None, config=None))
dev = sv.device
SR = 22050

# ---- reference voice: computed once ----
with torch.no_grad():
    ref = torch.tensor(librosa.load(REF, sr=SR)[0][: SR * 25]).unsqueeze(0).float().to(dev)
    ref16 = torchaudio.functional.resample(ref, SR, 16000)
    S_ori = semantic_fn(ref16)
    mel2 = mel_fn(ref.float())
    feat2 = torchaudio.compliance.kaldi.fbank(ref16, num_mel_bins=80, dither=0, sample_frequency=16000)
    style2 = campplus_model((feat2 - feat2.mean(dim=0, keepdim=True)).unsqueeze(0))
    prompt_cond, *_ = model.length_regulator(S_ori, ylens=torch.LongTensor([mel2.size(2)]).to(dev),
                                             n_quantizers=3, f0=None)
LOCK = threading.Lock()  # one conversion at a time on the GPU
print(f"READY seed-vc on {dev} in {time.time() - t0:.1f}s, ref={os.path.basename(REF)}", flush=True)


def convert(wav_bytes: bytes, steps: int) -> bytes:
    audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != SR:
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SR)
    with LOCK, torch.no_grad():
        src = torch.tensor(audio).unsqueeze(0).float().to(dev)
        S_alt = semantic_fn(torchaudio.functional.resample(src, SR, 16000))
        mel = mel_fn(src.float())
        cond, *_ = model.length_regulator(S_alt, ylens=torch.LongTensor([mel.size(2)]).to(dev),
                                          n_quantizers=3, f0=None)
        cat = torch.cat([prompt_cond, cond], dim=1)  # sentences are short: one window is enough
        out = model.cfm.inference(cat, torch.LongTensor([cat.size(1)]).to(dev), mel2, style2, None, steps,
                                  inference_cfg_rate=opts.cfg_rate)[:, :, mel2.size(-1):]
        wave = vocoder_fn(out.float()).squeeze().cpu().numpy()
    buf = io.BytesIO()
    sf.write(buf, wave, SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        ok = urlparse(self.path).path == "/health"
        self.send_response(200 if ok else 404)
        self.end_headers()
        self.wfile.write(b"ok" if ok else b"")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/convert":
            self.send_response(404); self.end_headers(); return
        steps = int(parse_qs(u.query).get("steps", [opts.steps])[0])
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        t = time.time()
        try:
            data = convert(body, steps)
        except Exception as e:  # report, don't crash the service
            self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode()[:500]); return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("X-Convert-Ms", str(int((time.time() - t) * 1000)))
        self.end_headers()
        self.wfile.write(data)


ThreadingHTTPServer(("127.0.0.1", opts.port), Handler).serve_forever()
