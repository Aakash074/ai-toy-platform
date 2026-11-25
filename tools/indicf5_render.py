"""Render many lines with IndicF5 in ONE model load (runs in .voiceclone-venv).

    .voiceclone-venv/bin/python tools/indicf5_render.py jobs.json
jobs.json: {"ref": "data/voices/x.wav", "ref_text": "...", "nfe": 32,
            "jobs": [{"text": "...", "out": "path.wav"}, ...]}
Prints one line per finished job: "OK <out> <seconds>" or "FAIL <out> <error>".
"""
import functools
import json
import os
import sys
import time

import numpy as np
import soundfile as sf
import torch
import torchaudio
from transformers import AutoModel


def _sf_load(path, *a, **k):  # newer torchaudio needs torchcodec for load(); soundfile is enough here
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return torch.from_numpy(data.T.copy()), sr


torchaudio.load = _sf_load

spec = json.load(open(sys.argv[1], encoding="utf-8"))
model = AutoModel.from_pretrained("ai4bharat/IndicF5", trust_remote_code=True)
mod = sys.modules[type(model).__module__]
mod.infer_process = functools.partial(mod.infer_process, nfe_step=int(spec.get("nfe", 32)))
print("READY", flush=True)
for job in spec["jobs"]:
    t = time.time()
    try:
        audio = model(job["text"], ref_audio_path=spec["ref"], ref_text=spec["ref_text"])
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        os.makedirs(os.path.dirname(job["out"]) or ".", exist_ok=True)
        sf.write(job["out"], np.asarray(audio, dtype=np.float32), 24000)
        print(f"OK {job['out']} {time.time() - t:.1f}", flush=True)
    except Exception as e:  # keep going with the other lines
        print(f"FAIL {job['out']} {type(e).__name__}: {str(e)[:200]}", flush=True)
