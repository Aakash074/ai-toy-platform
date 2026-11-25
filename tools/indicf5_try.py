"""Quick IndicF5 voice-clone test: speak lines in the reference voice and time each one.
Run with the separate env:  .voiceclone-venv/bin/python tools/indicf5_try.py REF.wav "REF TEXT" OUTDIR
"""
import sys, time
import numpy as np, soundfile as sf, torch
from transformers import AutoModel
import torchaudio


def _sf_load(path, *a, **k):  # newer torchaudio needs torchcodec for load(); soundfile is enough here
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return torch.from_numpy(data.T.copy()), sr


torchaudio.load = _sf_load

ref, ref_text, out = sys.argv[1], sys.argv[2], sys.argv[3]
LINES = [
    ("hi_1", "अरे राजू! सब कहाँ चले गए?"),
    ("hi_3", "बताओ, कैसे ठीक कर सकते हो?"),
    ("hi_new", "नमस्ते दोस्त! मैं छोटा भीम हूँ। चलो, आज मिलकर खूब मस्ती करते हैं!"),
    ("te_new", "నమస్తే మిత్రమా! నేను ఛోటా భీమ్‌ని. రా, ఈరోజు కలిసి ఆడుకుందాం!"),
]
t = time.time()
model = AutoModel.from_pretrained("ai4bharat/IndicF5", trust_remote_code=True)
import functools, os
dev = os.environ.get("F5_DEVICE") or ("mps" if torch.backends.mps.is_available() else "cpu")
model = model.to(dev)
NFE = int(os.environ.get("F5_NFE", "32"))  # diffusion steps: fewer = faster, slightly rougher
mod = sys.modules[type(model).__module__]
mod.infer_process = functools.partial(mod.infer_process, nfe_step=NFE)
print(f"loaded in {time.time()-t:.1f}s on {dev}, nfe={NFE}", flush=True)
for name, text in LINES:
    t = time.time()
    audio = model(text, ref_audio_path=ref, ref_text=ref_text)
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    sf.write(f"{out}/f5_{name}_{dev}_{NFE}.wav", np.asarray(audio, dtype=np.float32), 24000)
    print(f"{name}: {time.time()-t:.1f}s for {len(audio)/24000:.1f}s of audio", flush=True)
