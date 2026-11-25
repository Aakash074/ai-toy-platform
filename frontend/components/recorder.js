// Microphone capture → 16 kHz mono 16-bit WAV, plus a simple energy-based VAD for hands-free mode.
// No external libraries. Works on http://localhost (a secure context for getUserMedia).

export class Recorder {
  constructor({ targetRate = 16000, onLevel = () => {} } = {}) {
    this.targetRate = targetRate;
    this.onLevel = onLevel;
    this.ctx = null;
    this.stream = null;
    this.node = null;
    this.recording = false;
    this.chunks = [];
    this.preRoll = [];         // keeps ~300ms before VAD trigger so first syllable isn't clipped
    this.preRollMax = 6;
    this.listeners = new Set(); // per-chunk callbacks (VAD)
  }

  get ready() { return !!this.ctx; }

  async init() {
    if (this.ctx) { if (this.ctx.state === "suspended") await this.ctx.resume(); return; }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = this.ctx.createMediaStreamSource(this.stream);
    // ScriptProcessor is deprecated but universally supported and fine for a prototype.
    this.node = this.ctx.createScriptProcessor(2048, 1, 1);
    this.node.onaudioprocess = (e) => {
      const data = new Float32Array(e.inputBuffer.getChannelData(0));
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      const rms = Math.sqrt(sum / data.length);
      this.onLevel(rms);
      if (this.recording) this.chunks.push(data);
      else { this.preRoll.push(data); if (this.preRoll.length > this.preRollMax) this.preRoll.shift(); }
      for (const fn of this.listeners) fn(rms, data);
    };
    src.connect(this.node);
    // Route through a muted gain so the processor runs without echoing the mic to speakers.
    const mute = this.ctx.createGain(); mute.gain.value = 0;
    this.node.connect(mute); mute.connect(this.ctx.destination);
  }

  start({ withPreRoll = false } = {}) {
    this.chunks = withPreRoll ? [...this.preRoll] : [];
    this.recording = true;
    this.startedAt = performance.now();
  }

  cancel() { this.recording = false; this.chunks = []; }

  /** Stops and returns { blob, seconds }. */
  stop() {
    this.recording = false;
    const len = this.chunks.reduce((n, c) => n + c.length, 0);
    const merged = new Float32Array(len);
    let off = 0;
    for (const c of this.chunks) { merged.set(c, off); off += c.length; }
    this.chunks = [];
    const down = downsample(merged, this.ctx.sampleRate, this.targetRate);
    return { blob: encodeWav(down, this.targetRate), seconds: down.length / this.targetRate };
  }
}

function downsample(buf, from, to) {
  if (from === to) return buf;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(buf.length / ratio));
  for (let i = 0; i < out.length; i++) {
    // average the samples that fall into this output slot (cheap low-pass to limit aliasing)
    const s = Math.floor(i * ratio), e = Math.min(buf.length, Math.floor((i + 1) * ratio));
    let sum = 0; for (let j = s; j < e; j++) sum += buf[j];
    out[i] = sum / Math.max(1, e - s);
  }
  return out;
}

function encodeWav(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE");
  w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  w(36, "data"); v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

/** Energy VAD: speech starts after `startMs` above threshold, ends after `silenceMs` below it. */
export class VAD {
  constructor({ threshold = 0.02, startMs = 150, silenceMs = 900, chunkMs = 43 } = {}) {
    Object.assign(this, { threshold, startMs, silenceMs, chunkMs });
    this.reset();
  }
  reset() { this.speaking = false; this.above = 0; this.below = 0; }
  /** returns "start" | "end" | null */
  feed(rms) {
    if (!this.speaking) {
      this.above = rms > this.threshold ? this.above + this.chunkMs : 0;
      if (this.above >= this.startMs) { this.speaking = true; this.below = 0; return "start"; }
    } else {
      this.below = rms < this.threshold * 0.7 ? this.below + this.chunkMs : 0;
      if (this.below >= this.silenceMs) { this.reset(); return "end"; }
    }
    return null;
  }
}
