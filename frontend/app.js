import { Recorder, VAD } from "/static/components/recorder.js";

// ---------------------------------------------------------------- state
const S = {
  devices: new Map(),
  personalities: [],
  settings: null,
  selected: localGet("selectedDevice"),
  mode: "ptt",
  recording: false,
  busy: false,
  inFlight: new Set(),
  maxTimer: null,
  ws: null,
  wsRetry: 0,
  audio: { vad_threshold: +localGet("vad_threshold", 0.02), vad_silence: +localGet("vad_silence", 900) },
};
const STATES = ["BOOTING", "CONNECTING_WIFI", "CONNECTED", "LISTENING", "THINKING", "SPEAKING", "ERROR", "OFFLINE"];

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) n.append(k instanceof Node ? k : String(k));
  return n;
};
function localGet(k, d = null) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) { init.body = JSON.stringify(opts.json); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body?.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : `HTTP ${r.status}`);
  return body;
}

function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (isError ? " error" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3500);
}

function logEvent(text, kind = "") {
  const li = el("li", { class: kind }, el("time", {}, new Date().toLocaleTimeString()), text);
  const log = $("#eventLog");
  log.prepend(li);
  while (log.children.length > 200) log.lastChild.remove();
}

const fmtAgo = (ts) => {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? `${Math.floor(s)}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : new Date(ts * 1000).toLocaleString();
};

// ---------------------------------------------------------------- tabs
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${b.dataset.view}`));
  if (b.dataset.view === "devices") renderDeviceTable();
  if (b.dataset.view === "settings") loadSettings();
}));

// ---------------------------------------------------------------- websocket
function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard`);
  S.ws = ws;
  ws.onopen = () => { S.wsRetry = 0; $("#backendConn").classList.add("ok"); logEvent("dashboard connected to backend", "device"); };
  ws.onclose = () => {
    $("#backendConn").classList.remove("ok");
    const delay = Math.min(10000, 500 * 2 ** S.wsRetry++);
    logEvent(`backend connection lost; retrying in ${(delay / 1000).toFixed(1)}s`, "error");
    setTimeout(connectWS, delay);
  };
  ws.onmessage = (m) => handleEvent(JSON.parse(m.data));
}
function wsSend(obj) { if (S.ws?.readyState === 1) S.ws.send(JSON.stringify(obj)); }

function patchDevice(id, fields) {
  const d = S.devices.get(id);
  if (d) Object.assign(d, fields);
}

function handleEvent(e) {
  const mine = e.device_id && e.device_id === S.selected;
  switch (e.type) {
    case "hello":
      setDevices(e.devices); break;
    case "devices_changed":
    case "personalities_changed":
      refreshAll(); break;
    case "device_online":
      patchDevice(e.device_id, { online: true, state: "CONNECTED", firmware_version: e.firmware_version ?? S.devices.get(e.device_id)?.firmware_version });
      logEvent(`${e.device_id} online (fw ${e.firmware_version ?? "?"}, ip ${e.ip ?? "?"})`, "device");
      refreshAll(); break;
    case "device_offline":
      patchDevice(e.device_id, { online: false, state: "OFFLINE" });
      logEvent(`${e.device_id} offline (${e.reason ?? "disconnected"})`, "error"); break;
    case "heartbeat":
      patchDevice(e.device_id, { last_heartbeat: e.ts, wifi: { ...(S.devices.get(e.device_id)?.wifi || {}), rssi: e.rssi, connected: true }, uptime_s: e.uptime_s, free_heap: e.free_heap });
      break;
    case "device_state":
      patchDevice(e.device_id, { state: e.state });
      if (mine && e.stage) setStage(e.stage);
      break;
    case "button":
      logEvent(`${e.device_id} button ${e.action}`, "button");
      if (mine) {
        if (e.action === "press") startRecording("esp32");
        else if (e.action === "release") stopRecording();
      } else if (e.action === "press") toast(`Button pressed on ${e.device_id} (not the active device)`);
      break;
    case "transcript":
      if (mine) logEvent(`STT: “${e.text}”`, "ai"); break;
    case "turn":
      logEvent(`reply (${e.personality_id}) in ${e.timings_ms?.total ?? "?"}ms${e.flags?.length ? " flags=" + e.flags.join(",") : ""}`, e.error ? "error" : "ai");
      if (e.error) logEvent(e.error, "error");
      if (mine && !S.inFlight.has(e.device_id) && e.transcript) addTurn(e); // turns triggered elsewhere
      break;
    case "history_cleared":
      if (mine) loadHistory(); break;
    case "device_log":
      logEvent(`${e.device_id}: ${e.message}`, e.level === "error" ? "error" : "device"); break;
  }
  if (e.device_id && e.type !== "transcript") { renderDeviceSummary(); renderStateStrip(); }
}

// ---------------------------------------------------------------- devices + personalities
function setDevices(list) {
  S.devices = new Map(list.map((d) => [d.id, d]));
  if (!S.selected || !S.devices.has(S.selected)) S.selected = list.find((d) => !d.is_virtual && d.online)?.id || list[0]?.id || null;
  renderDeviceSelect(); renderDeviceSummary(); renderStateStrip(); renderPersonalitySelect();
  if (document.querySelector('.tab.active').dataset.view === "devices") renderDeviceTable();
}

async function refreshAll() {
  try {
    const [devs, pers] = await Promise.all([api("/api/devices"), api("/api/personalities")]);
    S.personalities = pers;
    setDevices(devs);
  } catch (err) { toast(err.message, true); }
}

function renderDeviceSelect() {
  const sel = $("#deviceSelect");
  sel.replaceChildren(...[...S.devices.values()].map((d) =>
    el("option", { value: d.id, selected: d.id === S.selected },
      `${d.online ? "●" : "○"} ${d.name} ${d.is_virtual ? "(virtual)" : ""}`)));
}

$("#deviceSelect").addEventListener("change", (e) => {
  S.selected = e.target.value; localSet("selectedDevice", S.selected);
  renderDeviceSummary(); renderStateStrip(); renderPersonalitySelect(); loadHistory();
  logEvent(`active device → ${S.selected}`);
});

function renderDeviceSummary() {
  const d = S.devices.get(S.selected);
  const box = $("#deviceSummary");
  if (!d) { box.replaceChildren(el("dt", {}, "No device")); return; }
  const rows = [
    ["Device ID", d.id],
    ["Type", d.is_virtual ? "virtual" : d.hardware],
    ["Status", d.online ? "online" : "offline"],
    ["Firmware", d.firmware_version],
    ["Wi-Fi RSSI", d.is_virtual ? "—" : (d.wifi?.rssi != null ? `${d.wifi.rssi} dBm` : "—")],
    ["Heartbeat", d.is_virtual ? "—" : fmtAgo(d.last_heartbeat)],
  ];
  box.replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", { title: v }, v)]));
  $("#brandLed").style.background = d.online ? "var(--accent)" : "var(--danger)";
}
setInterval(renderDeviceSummary, 5000);

function renderStateStrip() {
  const d = S.devices.get(S.selected);
  $("#stateStrip").replaceChildren(...STATES.map((s) =>
    el("div", { class: `state s-${s}${d?.state === s ? " on" : ""}` }, el("span", { class: "led" }), s)));
}

function renderPersonalitySelect() {
  const d = S.devices.get(S.selected);
  $("#personalitySelect").replaceChildren(...S.personalities.map((p) =>
    el("option", { value: p.id, selected: d?.personality_id === p.id }, `${p.name} · ages ${p.age_group}`)));
}

$("#personalitySelect").addEventListener("change", async (e) => {
  try {
    await api(`/api/devices/${encodeURIComponent(S.selected)}`, { method: "PATCH", json: { personality_id: e.target.value } });
    patchDevice(S.selected, { personality_id: e.target.value });
    const p = S.personalities.find((x) => x.id === e.target.value);
    logEvent(`personality → ${p?.name}`, "device");
    toast(`${S.selected} is now ${p?.name}`);
    loadHistory();
  } catch (err) { toast(err.message, true); }
});

// ---------------------------------------------------------------- conversation history
async function loadHistory() {
  if (!S.selected) return;
  try {
    const rows = await api(`/api/devices/${encodeURIComponent(S.selected)}/conversations?limit=50`);
    const h = $("#history");
    h.replaceChildren();
    if (!rows.length) h.append(el("div", { class: "empty" }, "No conversation yet. Hold the button and say hello!"));
    rows.forEach((r) => addTurn({ transcript: r.user_text, reply: r.reply_text, flags: r.safety_flags,
      timings_ms: r.latency_ms, personality_id: r.personality_id, created_at: r.created_at }, false));
  } catch (err) { toast(err.message, true); }
}

function addTurn(t, scroll = true, audioUrl = null) {
  const h = $("#history");
  h.querySelector(".empty")?.remove();
  const pname = S.personalities.find((p) => p.id === t.personality_id)?.name ?? t.personality_id;
  if (t.transcript) h.append(el("div", { class: "msg child" }, t.transcript, el("div", { class: "meta" }, "child")));
  const meta = el("div", { class: "meta" }, pname);
  if (t.timings_ms?.total) meta.append(el("span", {}, `${t.timings_ms.total}ms (stt ${t.timings_ms.stt ?? "-"} · llm ${t.timings_ms.llm ?? "-"} · tts ${t.timings_ms.tts ?? "-"})`));
  (t.flags || []).forEach((f) => meta.append(el("span", { class: "flag" }, `⚑ ${f}`)));
  if (t.error) meta.append(el("span", { class: "err" }, t.error));
  if (audioUrl) meta.append(el("button", { class: "replay", onclick: () => new Audio(audioUrl).play() }, "replay"));
  h.append(el("div", { class: "msg toy" }, t.reply, meta));
  if (scroll) h.scrollTop = h.scrollHeight;
}

// ---------------------------------------------------------------- audio pipeline
const recorder = new Recorder({ onLevel: (rms) => { $("#meter").style.width = `${Math.min(100, rms * 600)}%`; } });
const vad = new VAD({ threshold: S.audio.vad_threshold, silenceMs: S.audio.vad_silence });
const ptt = $("#pttBtn");

// Demo opener: the character's pre-recorded greeting (expressive voice when it has been recorded).
$("#greetBtn")?.addEventListener("click", async () => {
  if (!S.selected) return toast("Select a device first", true);
  if (S.busy) return;
  S.busy = true;
  try {
    const r = await api(`/api/conversation/${encodeURIComponent(S.selected)}/greet`, { method: "POST" });
    let url = null;
    if (r.audio_b64) {
      const bytes = Uint8Array.from(atob(r.audio_b64), (ch) => ch.charCodeAt(0));
      url = URL.createObjectURL(new Blob([bytes], { type: r.audio_mime }));
    }
    logEvent(`greeting (${r.engine === "voice_cache" ? "recorded voice" : "live voice"})`);
    await playReply(url, r.text, r.language);
  } catch (err) { toast(err.message, true); }
  finally { S.busy = false; wsSend({ type: "playback_finished", device_id: S.selected }); setStage(null); idlePtt(); }
});
ptt.disabled = false;

function setStage(stage) {
  const order = ["listening", "stt", "llm", "tts", "speaking"];
  const idx = order.indexOf(stage);
  document.querySelectorAll("#pipeline span").forEach((s) => {
    const i = order.indexOf(s.dataset.stage);
    s.classList.toggle("active", i === idx);
    s.classList.toggle("done", idx >= 0 && i < idx);
  });
}

function setPtt(label, sub) {
  ptt.querySelector(".ptt-label").textContent = label;
  ptt.querySelector(".ptt-sub").textContent = sub;
  ptt.classList.toggle("recording", S.recording);
  ptt.classList.toggle("busy", S.busy);
}
const idlePtt = () => setPtt(S.mode === "ptt" ? "Hold to talk" : "Hands-free on", S.mode === "ptt" ? "or hold Space / ESP32 button" : "just start speaking");

async function enableAudio() {
  try {
    await recorder.init();
    recorder.listeners.add(onVadChunk);
    idlePtt();
    logEvent("microphone ready", "device");
    return true;
  } catch (err) {
    toast(`Microphone unavailable: ${err.message}`, true);
    return false;
  }
}

function startRecording(source = "ui", withPreRoll = true) {
  if (S.recording || S.busy) return;
  if (!S.selected) return toast("Select a device first", true);
  if (!recorder.ready) { toast("Click “Enable microphone” on the console first", true); return; }
  recorder.start({ withPreRoll });
  S.recording = true;
  setStage("listening"); setPtt("Listening…", "release to send");
  wsSend({ type: "recording_started", device_id: S.selected });
  logEvent(`recording (${source})`, "button");
  const max = (S.settings?.audio?.max_record_seconds || 15) * 1000;
  clearTimeout(S.maxTimer);
  S.maxTimer = setTimeout(() => { if (S.recording) { logEvent("max recording length reached"); stopRecording(); } }, max);
}

function stopRecording() {
  if (!S.recording || S.stopping) return;
  clearTimeout(S.maxTimer);
  // People let go of the button while finishing the last word: keep recording a moment longer.
  S.stopping = true;
  setTimeout(finishRecording, 350);
}

function finishRecording() {
  S.stopping = false;
  S.recording = false;
  const { blob, seconds } = recorder.stop();
  // Local-only playback of the last capture (never uploaded anywhere except the normal turn).
  try {
    const a = $("#lastRec"); if (a) { if (a.src) URL.revokeObjectURL(a.src); a.src = URL.createObjectURL(blob); $("#lastRecWrap").hidden = false; }
  } catch {}
  if (seconds < 0.4) {
    wsSend({ type: "recording_cancelled", device_id: S.selected });
    setStage(null); idlePtt();
    toast("Too short — hold the button while you speak");
    return;
  }
  logEvent(`captured ${seconds.toFixed(1)}s audio (${Math.round(blob.size / 1024)} KB)`);
  sendTurn(blob);
}

// Plays sentence clips in order as they arrive, with a short natural pause between sentences.
function sentencePlayer(gapMs = 320) {
  const queue = []; let wake = null; let ended = false; let count = 0;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const done = (async () => {
    for (;;) {
      if (!queue.length) { if (ended) return; await new Promise((r) => (wake = r)); continue; }
      const c = queue.shift();
      if (count++ > 0) await sleep(gapMs);
      await playReply(c.url, c.text, c.lang);
    }
  })();
  const poke = () => { if (wake) { const w = wake; wake = null; w(); } };
  return { push(c) { queue.push(c); poke(); }, end() { ended = true; poke(); }, done };
}

async function sendTurn(blob, text = null) {
  const did = S.selected;
  S.busy = true; S.inFlight.add(did);
  setStage("stt"); setPtt("Thinking…", "processing on backend");
  const player = sentencePlayer();
  const parts = []; let mime = "audio/mpeg";
  try {
    const base = `/api/conversation/${encodeURIComponent(did)}`;
    let init;
    if (text !== null) init = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) };
    else { const fd = new FormData(); fd.append("audio", blob, "speech.wav"); init = { method: "POST", body: fd }; }
    const r = await fetch(`${base}/${text !== null ? "text_stream" : "turn_stream"}`, init);
    if (!r.ok) {
      const b = r.headers.get("content-type")?.includes("json") ? await r.json() : {};
      throw new Error(typeof b.detail === "string" ? b.detail : `HTTP ${r.status}`);
    }
    const reader = r.body.getReader(); const dec = new TextDecoder();
    let buf = "", result = null;
    const handle = (ev) => {
      if (ev.type === "transcript") { setStage("llm"); logEvent(`heard: ${ev.text}`); }
      else if (ev.type === "audio") {
        let url = null;
        if (ev.audio_b64) {
          const bytes = Uint8Array.from(atob(ev.audio_b64), (ch) => ch.charCodeAt(0));
          parts.push(bytes); mime = ev.audio_mime || mime;
          url = URL.createObjectURL(new Blob([bytes], { type: ev.audio_mime }));
        }
        player.push({ url, text: ev.text, lang: ev.language });
      } else if (ev.type === "error") throw new Error(ev.detail || "turn failed");
      else if (ev.type === "done") result = ev;
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (line.trim()) handle(JSON.parse(line));
      }
    }
    player.end();
    if (result) {
      const all = parts.length ? URL.createObjectURL(new Blob(parts, { type: mime })) : null; // for "replay"
      addTurn(result, true, all);
    }
    await player.done;
  } catch (err) {
    player.end();
    toast(err.message, true);
    logEvent(`turn failed: ${err.message}`, "error");
    wsSend({ type: "recording_cancelled", device_id: did });
  } finally {
    S.busy = false; S.inFlight.delete(did);
    wsSend({ type: "playback_finished", device_id: did });
    setStage(null); idlePtt(); vad.reset();
  }
}

// Personality language code → BCP-47 tag for the browser speech fallback.
const SPEECH_LANG = { en: "en-US", hi: "hi-IN", te: "te-IN" };

function playReply(url, text, lang = "en") {
  setStage("speaking"); setPtt("Speaking…", "toy is talking");
  return new Promise((resolve) => {
    if (url) {
      const a = new Audio(url);
      a.onended = resolve; a.onerror = resolve;
      a.play().catch((e) => { toast(`Playback blocked: ${e.message}`, true); resolve(); });
    } else if ("speechSynthesis" in window) { // TTS provider failed → browser fallback voice
      const u = new SpeechSynthesisUtterance(text);
      const tag = SPEECH_LANG[(lang || "en").split("-")[0]] || lang;
      u.lang = tag;
      const v = speechSynthesis.getVoices().find((x) => x.lang?.replace("_", "-").startsWith(tag.split("-")[0]));
      if (v) u.voice = v;
      u.onend = resolve; u.onerror = resolve;
      speechSynthesis.speak(u);
    } else resolve();
  });
}

function onVadChunk(rms) {
  if (S.mode !== "handsfree" || S.busy) return;
  const ev = vad.feed(rms);
  if (ev === "start" && !S.recording) startRecording("vad", true);
  else if (ev === "end" && S.recording) stopRecording();
}

// push-to-talk: mouse/touch/keyboard
ptt.addEventListener("pointerdown", async (e) => {
  e.preventDefault();
  if (!recorder.ready) { await enableAudio(); return; }
  if (S.mode === "ptt") { ptt.setPointerCapture(e.pointerId); startRecording("ui"); }
});
ptt.addEventListener("pointerup", () => { if (S.mode === "ptt") stopRecording(); });
ptt.addEventListener("pointercancel", () => { if (S.mode === "ptt") stopRecording(); });
const typing = () => ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
window.addEventListener("keydown", (e) => {
  if (e.code === "Space" && !e.repeat && !typing() && $("#view-console").classList.contains("active")) { e.preventDefault(); startRecording("space"); }
});
window.addEventListener("keyup", (e) => { if (e.code === "Space" && !typing()) stopRecording(); });

document.querySelectorAll('input[name="mode"]').forEach((r) => r.addEventListener("change", async (e) => {
  S.mode = e.target.value; vad.reset();
  if (S.mode === "handsfree" && !recorder.ready) await enableAudio();
  idlePtt(); logEvent(`mode → ${S.mode === "ptt" ? "push-to-talk" : "hands-free (VAD)"}`);
}));

$("#textForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("#textInput").value.trim();
  if (!t || S.busy) return;
  $("#textInput").value = "";
  sendTurn(null, t);
});

// ---------------------------------------------------------------- devices screen
function renderDeviceTable() {
  const pname = (id) => S.personalities.find((p) => p.id === id)?.name ?? id;
  $("#deviceRows").replaceChildren(...[...S.devices.values()].map((d) => el("tr", { onclick: () => showDeviceDetail(d.id) },
    el("td", {}, el("span", { class: `pill ${d.is_virtual ? "virtual" : d.online ? "on" : "off"}` }, d.is_virtual ? "virtual" : d.online ? d.state : "offline")),
    el("td", {}, d.id), el("td", {}, d.name), el("td", {}, d.firmware_version),
    el("td", {}, d.is_virtual ? "—" : d.online ? `${d.wifi?.rssi ?? "?"} dBm` : "disconnected"),
    el("td", {}, d.is_virtual ? "—" : fmtAgo(d.last_heartbeat)),
    el("td", {}, pname(d.personality_id)),
    el("td", {}, `${d.usage?.turns ?? 0} turns · ${d.usage?.conversation_minutes ?? 0} min`),
    el("td", {}, el("button", { class: "secondary", onclick: (ev) => { ev.stopPropagation(); S.selected = d.id; localSet("selectedDevice", d.id); renderDeviceSelect(); renderDeviceSummary(); renderStateStrip(); renderPersonalitySelect(); loadHistory(); document.querySelector('.tab[data-view="console"]').click(); } }, "Use")),
  )));
}

async function showDeviceDetail(id) {
  const box = $("#deviceDetail");
  let d;
  try { d = await api(`/api/devices/${encodeURIComponent(id)}`); } catch (err) { return toast(err.message, true); }
  const u = d.usage;
  const stat = (v, l) => el("div", { class: "stat" }, el("b", {}, v), el("span", {}, l));
  const form = el("form", { class: "form-grid" },
    el("label", { class: "field" }, el("span", {}, "Name"), el("input", { name: "name", value: d.name })),
    el("label", { class: "field" }, el("span", {}, "Personality"),
      el("select", { name: "personality_id" }, ...S.personalities.map((p) => el("option", { value: p.id, selected: p.id === d.personality_id }, p.name)))),
    el("label", { class: "field" }, el("span", {}, "Daily minutes limit"), el("input", { name: "daily_minutes_limit", type: "number", min: 0, value: d.daily_minutes_limit })),
    el("label", { class: "field" }, el("span", {}, "Daily turns limit"), el("input", { name: "daily_turns_limit", type: "number", min: 0, value: d.daily_turns_limit })),
    el("label", { class: "field" }, el("span", {}, "Quiet hours start"), el("input", { name: "quiet_hours_start", type: "time", value: d.quiet_hours_start || "" })),
    el("label", { class: "field" }, el("span", {}, "Quiet hours end"), el("input", { name: "quiet_hours_end", type: "time", value: d.quiet_hours_end || "" })),
    el("label", { class: "field check full" }, el("input", { name: "store_transcripts", type: "checkbox", checked: d.store_transcripts }), el("span", {}, "Keep conversation transcripts (text only)")),
    el("label", { class: "field" }, el("span", {}, "Child's first name / nickname (optional)"), el("input", { name: "child_name", maxlength: 20, value: d.child_name || "" })),
    el("label", { class: "field" }, el("span", {}, "Child's age (optional)"), el("input", { name: "child_age", type: "number", min: 0, max: 12, value: d.child_age || "" })),
    el("label", { class: "field full" }, el("span", {}, "Likes (comma separated, optional)"), el("input", { name: "child_interests", maxlength: 200, value: d.child_interests || "", placeholder: "dinosaurs, mangoes, cricket" })),
    el("div", { class: "field full" }, el("span", {}, "What the toy has learned"),
      el("code", {}, (() => { const m = d.child_memory || {}; const parts = [];
        if (m.name) parts.push(`name: ${m.name}`); if (m.age) parts.push(`age: ${m.age}`);
        if ((m.likes || []).length) parts.push(`likes: ${m.likes.join(", ")}`);
        if ((m.taught || []).length) parts.push(`taught: ${m.taught.join(", ")}`);
        parts.push(`chats: ${m.turns || 0}`); return parts.join(" · "); })())),
  );
  const save = async () => {
    const f = new FormData(form);
    const body = { name: f.get("name"), personality_id: f.get("personality_id"),
      daily_minutes_limit: +f.get("daily_minutes_limit"), daily_turns_limit: +f.get("daily_turns_limit"),
      quiet_hours_start: f.get("quiet_hours_start") || "", quiet_hours_end: f.get("quiet_hours_end") || "",
      store_transcripts: f.get("store_transcripts") === "on",
      child_name: (f.get("child_name") || "").trim(), child_age: +f.get("child_age") || 0,
      child_interests: (f.get("child_interests") || "").trim() };
    try { await api(`/api/devices/${encodeURIComponent(id)}`, { method: "PATCH", json: body }); toast("Saved"); refreshAll(); }
    catch (err) { toast(err.message, true); }
  };
  const cmd = (type) => api(`/api/devices/${encodeURIComponent(id)}/command`, { method: "POST", json: { type } })
    .then((r) => toast(r.sent ? `${type} sent` : "Device not connected", !r.sent)).catch((e) => toast(e.message, true));

  box.hidden = false;
  box.replaceChildren(
    el("div", { class: "row-head" }, el("h2", {}, `Device · ${d.id}`),
      el("div", { class: "btnrow" },
        !d.is_virtual && el("button", { class: "secondary", onclick: () => cmd("identify") }, "Blink LED"),
        !d.is_virtual && el("button", { class: "secondary", onclick: () => cmd("ping") }, "Ping"),
        el("button", { class: "danger", onclick: async () => {
          if (!confirm(`Delete ALL conversation history for ${id}?`)) return;
          const r = await api(`/api/devices/${encodeURIComponent(id)}/conversations`, { method: "DELETE" });
          toast(`Deleted ${r.deleted} turns`); loadHistory(); } }, "Delete history"),
        el("button", { class: "danger", onclick: async () => {
          if (!confirm(`Make the toy forget everything it learned about the child on ${id}?`)) return;
          await api(`/api/devices/${encodeURIComponent(id)}`, { method: "PATCH", json: { reset_child_memory: true } });
          toast("Toy memory erased"); showDeviceDetail(id); } }, "Forget child"),
        el("button", { class: "danger", onclick: async () => {
          if (!confirm(`Remove device ${id}? (history is deleted too)`)) return;
          await api(`/api/devices/${encodeURIComponent(id)}`, { method: "DELETE" }); box.hidden = true; refreshAll(); } }, "Remove device"))),
    el("div", { class: "detail-grid" },
      el("div", {},
        el("dl", { class: "kv" }, ...[
          ["Device ID", d.id], ["Hardware", d.hardware], ["MAC", d.mac || "—"], ["Firmware", d.firmware_version],
          ["Wi-Fi", d.is_virtual ? "n/a (virtual)" : d.online ? `connected · ${d.wifi?.rssi ?? "?"} dBm · ${d.wifi?.ip ?? ""}` : "disconnected"],
          ["Last heartbeat", d.is_virtual ? "—" : fmtAgo(d.last_heartbeat)],
          ["Uptime", d.uptime_s != null ? `${d.uptime_s}s` : "—"], ["Free heap", d.free_heap != null ? `${d.free_heap} B` : "—"],
          ["Parent account", d.account_id || "— (unassigned)"],
        ].flatMap(([k, v]) => [el("dt", {}, k), el("dd", { title: v }, v)]))),
      el("div", {},
        el("h2", {}, "Usage"),
        el("div", { class: "stat-grid" },
          stat(u.today.turns, "turns today"), stat(u.today.conversation_minutes, "min today"), stat(u.month.turns, "turns this month"),
          stat(u.month.llm_input_tokens + u.month.llm_output_tokens, "LLM tokens / month"), stat(u.month.stt_seconds, "STT sec / month"), stat(u.month.tts_chars, "TTS chars / month"))),
      el("div", {}, el("h2", {}, "Settings & parental controls"), form, el("button", { onclick: save }, "Save device")),
    ),
  );
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

$("#newDeviceForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/devices", { method: "POST", json: { device_id: f.get("device_id"), name: f.get("name") || null } });
    e.target.reset(); toast("Virtual device added"); refreshAll();
  } catch (err) { toast(err.message, true); }
});

// ---------------------------------------------------------------- settings screen
const PROVIDERS = {
  llm_provider: ["openai_compat", "anthropic", "mock"],
  stt_provider: ["mlx_whisper", "faster_whisper", "openai_compat", "mock"],
  tts_provider: ["gemini_tts", "edge_tts", "macos_say", "openai_compat", "mock"],
};

async function loadSettings() {
  try {
    S.settings = await api("/api/settings");
  } catch (err) { return toast(err.message, true); }
  const s = S.settings;
  const LOCAL = ["mlx_whisper", "faster_whisper", "macos_say", "edge_tts", "mock"];
  const key = (ok, provider) => LOCAL.includes(provider) ? el("span", { class: "muted" }, "local · no key needed")
    : el("span", { class: ok ? "badge-ok" : "badge-bad" }, ok ? "✓ key set in env" : "no key in env (fine for local servers like Ollama)");
  const sel = (name, val) => el("select", { name }, ...PROVIDERS[name].map((p) => el("option", { value: p, selected: p === val }, p)));
  const inp = (name, val, type = "text") => el("input", { name, value: val ?? "", type });
  const f = (label, input, extra) => el("label", { class: "field" }, el("span", {}, label, extra ? " · " : "", extra ?? ""), input);
  $("#providerForm").replaceChildren(
    f("LLM provider", sel("llm_provider", s.llm.provider), key(s.llm.api_key_configured, s.llm.provider)),
    f("LLM base URL (openai_compat)", inp("llm_base_url", s.llm.base_url)),
    f("LLM model", inp("llm_model", s.llm.model)),
    f("LLM history turns", inp("llm_history_turns", s.llm.history_turns, "number")),
    f("STT provider", sel("stt_provider", s.stt.provider), key(s.stt.api_key_configured, s.stt.provider)),
    f("STT model", inp("stt_model", s.stt.model)),
    f("STT multilingual model (non-English personalities)", inp("stt_multilingual_model", s.stt.multilingual_model)),
    f("TTS provider", sel("tts_provider", s.tts.provider), key(s.tts.api_key_configured, s.tts.provider)),
    f("TTS model", inp("tts_model", s.tts.model)),
  );
  const af = $("#audioForm");
  af.max_record_seconds.value = s.audio.max_record_seconds;
  af.save_raw_audio.checked = s.audio.save_raw_audio;
  af.vad_threshold.value = S.audio.vad_threshold;
  af.vad_silence.value = S.audio.vad_silence;
  $("#safetyPreamble").textContent = s.safety_preamble;
  renderPersonalityEditor();
}

$("#saveProviders").addEventListener("click", async () => {
  const body = Object.fromEntries(new FormData($("#providerForm")));
  body.llm_history_turns = +body.llm_history_turns;
  try { await api("/api/settings", { method: "PATCH", json: body }); toast("Providers updated"); loadSettings(); }
  catch (err) { toast(err.message, true); }
});

$("#testProviders").addEventListener("click", async () => {
  const out = $("#providerTest");
  out.textContent = "Testing… (first STT run may download/load the model)";
  try {
    const r = await api("/api/settings/test", { method: "POST" });
    out.textContent = Object.entries(r).map(([k, v]) => `${v.ok ? "✓" : "✗"} ${k.toUpperCase()} ${v.ms}ms — ${v.detail}`).join("\n");
  } catch (err) { out.textContent = err.message; }
});

$("#saveAudio").addEventListener("click", async () => {
  const af = $("#audioForm");
  S.audio.vad_threshold = +af.vad_threshold.value; S.audio.vad_silence = +af.vad_silence.value;
  vad.threshold = S.audio.vad_threshold; vad.silenceMs = S.audio.vad_silence;
  localSet("vad_threshold", S.audio.vad_threshold); localSet("vad_silence", S.audio.vad_silence);
  try {
    S.settings = { ...S.settings, ...(await api("/api/settings", { method: "PATCH",
      json: { max_record_seconds: +af.max_record_seconds.value, save_raw_audio: af.save_raw_audio.checked } })) };
    toast("Audio settings saved");
  } catch (err) { toast(err.message, true); }
});

let editingPid = null;
function renderPersonalityEditor(pid = editingPid) {
  const sel = $("#personalityEditSelect");
  editingPid = pid && (pid === "__new" || S.personalities.some((p) => p.id === pid)) ? pid : S.personalities[0]?.id;
  sel.replaceChildren(...S.personalities.map((p) => el("option", { value: p.id, selected: p.id === editingPid }, p.name)),
    editingPid === "__new" ? el("option", { value: "__new", selected: true }, "(new)") : null);
  const p = S.personalities.find((x) => x.id === editingPid) || { id: "", name: "", age_group: "4-8", language: "en",
    response_length: { max_sentences: 3, max_tokens: 150 }, teaching_style: "", system_prompt: "", voice: { macos_say: { voice: "Samantha", rate: 170 } } };
  const f = (label, input, cls = "") => el("label", { class: `field ${cls}` }, el("span", {}, label), input);
  $("#personalityForm").replaceChildren(
    f("ID", el("input", { name: "id", value: p.id, readonly: editingPid !== "__new", pattern: "[A-Za-z0-9_\\-]{1,40}", required: true })),
    f("Name", el("input", { name: "name", value: p.name, required: true })),
    f("Age group", el("input", { name: "age_group", value: p.age_group })),
    f("Language", el("input", { name: "language", value: p.language })),
    f("Max sentences", el("input", { name: "max_sentences", type: "number", min: 1, max: 10, value: p.response_length.max_sentences })),
    f("Max tokens", el("input", { name: "max_tokens", type: "number", min: 16, max: 1024, value: p.response_length.max_tokens })),
    f("Teaching style", el("input", { name: "teaching_style", value: p.teaching_style }), "full"),
    f("System prompt", el("textarea", { name: "system_prompt" }, p.system_prompt), "full"),
    f("Voice config (JSON, per TTS provider)", el("textarea", { name: "voice", style: "min-height:70px" }, JSON.stringify(p.voice, null, 1)), "full"),
  );
}
$("#personalityEditSelect").addEventListener("change", (e) => renderPersonalityEditor(e.target.value));
$("#newPersonality").addEventListener("click", () => renderPersonalityEditor("__new"));
$("#reloadPersonalities").addEventListener("click", async () => {
  try { await api("/api/personalities/reload", { method: "POST" }); await refreshAll(); renderPersonalityEditor(); toast("Reloaded from YAML"); }
  catch (err) { toast(err.message, true); }
});
$("#savePersonality").addEventListener("click", async () => {
  const f = new FormData($("#personalityForm"));
  let voice;
  try { voice = JSON.parse(f.get("voice") || "{}"); } catch { return toast("Voice config is not valid JSON", true); }
  const id = f.get("id").trim();
  const body = { name: f.get("name"), age_group: f.get("age_group"), language: f.get("language"),
    teaching_style: f.get("teaching_style"), system_prompt: f.get("system_prompt"), voice,
    response_length: { max_sentences: +f.get("max_sentences"), max_tokens: +f.get("max_tokens") } };
  try {
    await api(`/api/personalities/${encodeURIComponent(id)}`, { method: "PUT", json: body });
    await refreshAll(); renderPersonalityEditor(id); toast(`Saved ${body.name}`);
  } catch (err) { toast(err.message, true); }
});

// ---------------------------------------------------------------- boot
(async function boot() {
  connectWS();
  await refreshAll();
  try { S.settings = await api("/api/settings"); } catch {}
  loadHistory();
})();
