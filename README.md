# AI Toy Lab — Universal AI Toy prototype (V1)

A working end-to-end prototype of a conversational toy module:

```
CHILD ─▶ LAPTOP MIC ─▶ FastAPI BACKEND ─▶ STT ─▶ safety ─▶ LLM ─▶ safety ─▶ TTS ─▶ LAPTOP SPEAKER
                              ▲  │
                 WebSocket    │  ▼  set_state (LISTENING / THINKING / SPEAKING ...)
                        ESP32 toy controller  (Wi-Fi, device ID, status LED, talk button, BLE setup)
```

* **Laptop** = microphone + speaker + dashboard (browser page served by the backend).
* **Backend** = all AI (STT → LLM → TTS), devices, personalities, safety, usage tracking.
* **ESP32** = the toy's body: identity, Wi-Fi, heartbeat, LED state, physical talk button. No AI and no audio run on it in V1.

> ⚠️ This is a prototype. The safety layer is defence-in-depth, **not** certified, and the
> project makes **no claim of compliance** with child-privacy regulations (COPPA, GDPR-K, DPDP...).

---

## 1. Quick start (laptop only, ~5 minutes)

Requirements: macOS (for the free built-in `say` TTS), Python 3.12+ (3.14 tested).

```bash
cd ai-toy-platform
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # then pick an LLM (section 2)
.venv/bin/python -m backend.main
```

Open **http://localhost:8000**, click **Enable microphone** once (allow the browser prompt),
then **hold the button (or Space)** and talk. The first run downloads the Whisper
`base.en` STT model (~150 MB, once).

No LLM yet? Set `LLM_PROVIDER=mock` in `.env` to exercise the whole pipeline with canned
replies (STT and TTS are still real).

## 2. Choosing an LLM (all swappable in `.env` or the Settings screen)

| Option | Cost | `.env` |
|---|---|---|
| **Ollama** (local, offline) | free | `brew install ollama && ollama serve` then `ollama pull llama3.2:3b` · `LLM_PROVIDER=openai_compat` `LLM_BASE_URL=http://localhost:11434/v1` `LLM_MODEL=llama3.2:3b` |
| **Groq** (cloud, free tier) | free tier | `LLM_PROVIDER=openai_compat` `LLM_BASE_URL=https://api.groq.com/openai/v1` `LLM_MODEL=llama-3.1-8b-instant` `LLM_API_KEY=...` |
| **Claude** | paid | `LLM_PROVIDER=anthropic` `LLM_MODEL=claude-opus-5-5` `LLM_API_KEY=...` |
| Any OpenAI-compatible API | varies | `LLM_PROVIDER=openai_compat` + base URL / model / key |

STT: `faster_whisper` (local, default) or `openai_compat` (`/audio/transcriptions`; Groq's free tier works).
TTS: `macos_say` (local, default) or `openai_compat` (`/audio/speech`).

### Hindi & Telugu (personalities `gappu_hi`, `gappu_te`)
**Gappu the Baby Elephant** is an original cartoon character made for this project: a tiny,
sky-blue, chatty baby elephant whose catchphrase is a trumpet sound ("पोम-पोम-पम!" / "పోం-పోం-పం!").
It comes as two personalities, one per language. Their prompts make the LLM reply **only** in Hindi
(Devanagari script) or Telugu (Telugu script), using short sentences for ages 4–8. The safety rules
are still prepended first.

* **STT follows the personality's `language`.** `STTProvider.transcribe(wav, language)` gets the code.
  With `faster_whisper`, English personalities keep using `STT_MODEL` (`base.en`). Any other language
  switches to `STT_MULTILINGUAL_MODEL` and passes the language as a hint. The default is
  `large-v3-turbo`: a one-time download of about 1.6 GB, loaded on the first Hindi/Telugu turn,
  about 6 s of STT per turn on a Mac CPU. `openai_compat` sends `language=hi|te` to
  `/audio/transcriptions` (e.g. Groq's free `whisper-large-v3`).
  `STT_MULTILINGUAL_MODEL=small` (about 480 MB) is fine for Hindi, but in testing it wrote Telugu
  speech in Devanagari script.
* **TTS** stays offline on `macos_say`, using the built-in voices **Lekha** (hi_IN) and **Geeta**
  (te_IN). Check them with `say -v '?' | grep -E 'hi_IN|te_IN'`; if they are missing, install them
  under System Settings → Accessibility → Spoken Content → System voice → Manage Voices. Voices are
  set per personality and per provider in `config/personalities.yaml`. If a voice is missing,
  Indic text is **not** read by the English default voice. The turn reports `tts_failed`, and the
  dashboard falls back to browser `speechSynthesis` with `lang=hi-IN`/`te-IN`.
* **Safety:** the output filter keeps Devanagari and Telugu text, splits sentences on `। ॥ . ! ?`,
  and has a short Hindi/Telugu unsafe-word list. Fallback, "didn't hear you" and LLM-error replies
  are spoken in the personality's language.
* **LLM:** small local models (e.g. `llama3.2:3b`) write weak Hindi and poor Telugu. For good
  results, use a larger model (e.g. Claude, or Groq `llama-3.3-70b-versatile`). `LLM_PROVIDER=mock`
  returns canned Hindi/Telugu replies.
Keys are read **only** from environment variables and never shown in the UI.

## 3. ESP32 (Phase 2)

### 3.1 Hardware
Any ESP32 dev board + USB cable. No wiring needed for the default setup:

| Function | Default pin | Notes |
|---|---|---|
| Talk button | **GPIO0** (the **BOOT** button) | active-low, internal pull-up |
| Status LED | **GPIO2** (onboard LED on most "ESP32 DevKit V1" boards) | `-DLED_PIN=-1` if your board has none |

Pins are build flags in `firmware/esp32/platformio.ini` (`LED_PIN`, `LED_ACTIVE_HIGH`, `BUTTON_PIN`,
`BUTTON_ACTIVE_LOW`); envs are provided for `esp32dev` (tested build), `esp32-s3` and `esp32-c3`.
The ESP32 only joins **2.4 GHz** Wi-Fi.

### 3.2 Configure, build, flash
```bash
cd firmware/esp32
cp include/secrets.example.h include/secrets.h     # Wi-Fi + your laptop's LAN IP
ipconfig getifaddr en0                              # ← the laptop IP to put in BACKEND_HOST
pip install platformio                              # or: brew install platformio
pio run -e esp32dev                                 # build
ls /dev/cu.*                                        # find the board (cu.usbserial-* / cu.SLAB_* / cu.wchusbserial*)
pio run -e esp32dev -t upload --upload-port /dev/cu.usbserial-XXXX
pio device monitor -b 115200
```
If upload can't connect: hold **BOOT**, tap **EN/RST**, release BOOT. CH340/CP210x boards may need the USB-serial driver.

The backend must listen on the LAN (default `HOST=0.0.0.0`). If macOS asks
"Accept incoming network connections?" for Python, click **Allow**.

### 3.3 Provisioning without re-flashing
Values saved on the device (NVS) override `secrets.h`:
* **Serial console** (in the monitor): `ssid MyWifi` · `pass secret` · `server 192.168.0.9 8000` · `save`
  (also `status`, `forget`, `ble`, `reboot`).
* **BLE**: press EN, then within 1.5 s hold BOOT for 3 s (or type `ble`). The board advertises
  `AIToy-xxxxxx`; with a BLE app (e.g. nRF Connect) write the SSID/password/host/port characteristics
  then `1` to the commit characteristic (UUIDs in `include/config.h`). Wi-Fi is **off** during BLE
  setup, so the two radios never run at the same time.

### 3.4 LED patterns
| State | LED |
|---|---|
| BOOTING | solid |
| CONNECTING_WIFI | fast blink (also: Wi-Fi up but backend unreachable, retrying) |
| CONNECTED | short blip every 2 s |
| LISTENING | solid |
| THINKING | medium blink |
| SPEAKING | double blip |
| ERROR | triple flash + pause (auto-recovers) |
| BLE setup | long on / short off |

### 3.5 No hardware? Use the simulator
```bash
.venv/bin/python tools/device_simulator.py --id esp32-sim-01        # ENTER toggles the button
```
It speaks exactly the firmware protocol, including reconnect with backoff.

## 4. Demo script (Definition of Done)

1. Flash firmware, power the ESP32 → serial shows `[wifi] connected` and `[ws] registered`.
2. Dashboard → device appears **online** (auto-selected) with firmware, RSSI and heartbeat.
3. Click **Enable microphone** once.
4. Hold the **BOOT button on the ESP32** (or the on-screen button / Space) and speak; release.
   The LED goes LISTENING → THINKING → SPEAKING while the laptop answers out loud.
5. The conversation appears in the history, with per-stage latency.
6. Change **Personality** to *Educational Teacher*, ask "Can you help me count?", and get a teacher-style answer in a different voice.
7. Unplug / reset the ESP32: the dashboard shows OFFLINE; on power-up it reconnects by itself. Restarting the backend is also fine: device and dashboard both reconnect.

## 5. Tests

```bash
.venv/bin/python -m pytest -q                       # 30 offline tests (mock providers, temp DB)
.venv/bin/python -m backend.main &                  # then, against the running server:
.venv/bin/python tools/e2e_voice_test.py            # real STT/TTS: synthesized child speech → reply audio
.venv/bin/python tools/e2e_voice_test.py --play     # same, and play the replies
.venv/bin/python tools/device_simulator.py --script "wait:1,press,wait:3,release,wait:2,drop,wait:5"
cd firmware/esp32 && pio run -e esp32dev            # firmware compiles
```

## 6. What's in the box

```
backend/
  main.py                 FastAPI app, static dashboard, heartbeat watchdog
  config.py               all settings from env (.env); secrets never exposed
  context.py              wires services; hot-swaps AI providers
  api/                    devices, conversation, settings/personalities/accounts, WebSockets
  ai/{stt,llm,tts}/       provider adapters (local / cloud / mock) behind small interfaces
  services/conversation.py  the STT→LLM→TTS pipeline (timeouts, retry, fallbacks, logging)
  services/usage.py       usage events (device + account), plans/quotas (no payments)
  devices/registry.py     device records (SQLite) + live state
  personalities/          YAML-backed personality registry
  safety/guard.py         safety prompt, input/output screening, parental limits
frontend/                 dashboard (index.html, app.js, components/recorder.js)
firmware/esp32/           PlatformIO project (HAL: LED, button, audio interfaces; BLE/serial provisioning; OTA stub)
config/personalities.yaml Teddy, Teacher, Storyteller, Gappu (Hindi/Telugu) (edit here or in Settings)
tools/                    device simulator, end-to-end voice test
tests/                    pytest suite
```

**Frontend choice:** the spec suggested Streamlit. I used a small HTML/JS page served by FastAPI
instead, because Streamlit can't do true press-and-hold recording or react instantly to ESP32
button events over a WebSocket. It needs no build step and no extra dependencies.

### Device protocol (`ws://<host>:8000/ws/device/<device_id>[?token=]`)
Device → server: `hello` (first), `heartbeat` (every 10 s), `button {press|release}`, `state`, `log`, `pong`.
Server → device: `welcome`, `set_state`, `personality`, `identify`, `ping`, `ota_check`, `reboot`.
A connection silent for 30 s is dropped and the device is marked offline; a reconnecting device replaces its stale socket.
REST fallback: `POST /api/devices/register`; OTA check: `GET /api/ota/<id>?version=`.

### Child safety (built in, always on)
* Non-overridable safety rules prepended to every personality prompt (no personal-info requests, no violent/sexual content, no medical/dangerous instructions, no emotional dependency, never claim to be human, no secrets from parents).
* Input screening: phone/email/address the child says is **redacted before the LLM and the DB**; self-harm/abuse/danger/medical phrases get a fixed "tell a trusted grown-up" reply without calling the LLM.
* Output screening: unsafe words, personal-info requests and dependency phrases are replaced with a safe reply; length is trimmed; markdown and emoji are stripped.
* Parental controls per device: daily minutes, daily turns, quiet hours, transcript storage on/off.
* **Raw audio is never stored** unless `SAVE_RAW_AUDIO=true`. Transcripts can be deleted per device (Devices → *Delete history*) or per turn (`DELETE /api/conversations/{id}`).

### Subscription-ready data model (no payments)
`accounts` (parent) → `subscriptions` (plan, status, monthly minute/token quotas) and `devices.account_id`.
Every AI call writes a `usage_events` row tagged with **both** `device_id` and `account_id`, so device-level and user-level usage are separate. Quota enforcement exists behind `ENFORCE_QUOTAS=true`.

## 7. Known limitations (V1)
* Mic capture uses the browser's (deprecated but universal) `ScriptProcessorNode`; hands-free VAD is a simple energy threshold (tune it in Settings → Audio). Headphones help in hands-free mode.
* Keyword-based safety filters are a baseline, not a moderation model. Add a moderation API/classifier before any real-child testing.
* OTA is a check-only placeholder; the partition table already has two app slots.
* The firmware `esp32-s3`/`esp32-c3` envs are configured but only `esp32dev` was compile-tested.
* Single-user, unauthenticated dashboard intended for a trusted local network.
