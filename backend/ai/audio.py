"""Small WAV helpers (stdlib + numpy, no ffmpeg needed for the WAV path)."""
from __future__ import annotations

import io
import math
import struct
import wave

import numpy as np


class AudioFormatError(ValueError):
    pass


def wav_to_float32(wav_bytes: bytes, target_rate: int = 16000) -> tuple[np.ndarray, float]:
    """Decode 8/16/32-bit PCM WAV → mono float32 at target_rate. Returns (samples, duration_s)."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            n_ch, width, rate, n_frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n_frames)
    except (wave.Error, EOFError) as e:
        raise AudioFormatError(f"not a valid PCM WAV file: {e}") from e

    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise AudioFormatError(f"unsupported sample width {width}")

    if n_ch > 1:
        x = x.reshape(-1, n_ch).mean(axis=1)
    duration = len(x) / float(rate) if rate else 0.0
    if rate != target_rate and len(x) > 0:
        n_out = int(round(len(x) * target_rate / rate))
        x = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x).astype(np.float32)
    return x, duration


def wav_duration(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0


def make_tone_wav(seconds: float = 0.4, freq: float = 660.0, rate: int = 22050) -> bytes:
    """Short sine beep — used by the mock TTS so the pipeline is testable offline."""
    n = int(seconds * rate)
    frames = b"".join(
        struct.pack("<h", int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def median_f0(samples: np.ndarray, rate: int, fmin: float = 110.0, fmax: float = 650.0) -> float | None:
    """Median pitch (Hz) of the voiced parts of a clip, via per-frame normalized autocorrelation.
    Fast enough for a few seconds of speech (vectorized FFT). Returns None if nothing voiced."""
    x = samples.astype(np.float32)
    n, hop = int(0.04 * rate), int(0.02 * rate)
    if len(x) < n:
        return None
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop]
    frames = frames - frames.mean(axis=1, keepdims=True)
    energy = np.sqrt(np.mean(frames ** 2, axis=1))
    frames = frames[energy > max(0.02, np.percentile(energy, 40))]
    if not len(frames):
        return None
    spec = np.fft.rfft(frames * np.hanning(n), 2 * n, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :n]
    ac = ac / np.maximum(ac[:, :1], 1e-9)
    lo, hi = int(rate / fmax), int(rate / fmin)
    lag = np.argmax(ac[:, lo:hi], axis=1) + lo
    strength = ac[np.arange(len(lag)), lag]
    f0 = rate / lag[strength > 0.45]
    return float(np.median(f0)) if len(f0) else None
