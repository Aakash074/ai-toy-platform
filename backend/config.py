"""Central configuration. All values come from environment variables (.env supported).

Secrets (API keys) are ONLY read from the environment — never hardcoded, never
written to disk, never returned by the API (only "configured: true/false").
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    # --- LLM ---
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "openai_compat"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "http://localhost:11434/v1"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "llama3.2:3b"))
    llm_timeout_s: float = field(default_factory=lambda: _env_float("LLM_TIMEOUT_S", 30.0))
    llm_history_turns: int = field(default_factory=lambda: _env_int("LLM_HISTORY_TURNS", 6))

    # --- STT ---
    stt_provider: str = field(default_factory=lambda: _env("STT_PROVIDER", "faster_whisper"))
    stt_api_key: str = field(default_factory=lambda: _env("STT_API_KEY"))
    stt_base_url: str = field(default_factory=lambda: _env("STT_BASE_URL", "https://api.openai.com/v1"))
    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "base.en"))
    # faster_whisper only: model used when the active personality's language isn't English and
    # STT_MODEL is English-only (*.en). Default large-v3-turbo ≈ 1.6 GB download (the smallest that
    # wrote Telugu in Telugu script in our tests); "small" ≈ 480 MB is fine for Hindi only.
    stt_multilingual_model: str = field(default_factory=lambda: _env("STT_MULTILINGUAL_MODEL", "large-v3-turbo"))
    stt_mlx_model: str = field(default_factory=lambda: _env("STT_MLX_MODEL", "mlx-community/whisper-large-v3-turbo"))
    stt_timeout_s: float = field(default_factory=lambda: _env_float("STT_TIMEOUT_S", 30.0))

    # --- TTS ---
    tts_provider: str = field(default_factory=lambda: _env("TTS_PROVIDER", "macos_say"))
    tts_api_key: str = field(default_factory=lambda: _env("TTS_API_KEY"))
    tts_base_url: str = field(default_factory=lambda: _env("TTS_BASE_URL", "https://api.openai.com/v1"))
    tts_model: str = field(default_factory=lambda: _env("TTS_MODEL", "tts-1"))
    tts_timeout_s: float = field(default_factory=lambda: _env_float("TTS_TIMEOUT_S", 30.0))
    # Gemini TTS (TTS_PROVIDER=gemini_tts): acted, style-prompted voices; falls back to edge_tts
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_tts_model: str = field(default_factory=lambda: _env("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"))

    # --- Audio ---
    max_record_seconds: int = field(default_factory=lambda: _env_int("MAX_RECORD_SECONDS", 15))
    sample_rate: int = 16000
    save_raw_audio: bool = field(default_factory=lambda: _env_bool("SAVE_RAW_AUDIO", False))

    # --- Server / device ---
    backend_url: str = field(default_factory=lambda: _env("BACKEND_URL", "http://localhost:8000"))
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    default_device_id: str = field(default_factory=lambda: _env("DEVICE_ID", "virtual-toy-01"))
    heartbeat_timeout_s: int = field(default_factory=lambda: _env_int("HEARTBEAT_TIMEOUT_S", 30))
    device_token: str = field(default_factory=lambda: _env("DEVICE_TOKEN"))  # optional shared secret

    # --- Storage ---
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", str(ROOT_DIR / "data" / "ai_toy.db"))))
    personalities_path: Path = field(
        default_factory=lambda: Path(_env("PERSONALITIES_PATH", str(ROOT_DIR / "config" / "personalities.yaml")))
    )

    # --- Subscriptions (architecture only, no billing in V1) ---
    enforce_quotas: bool = field(default_factory=lambda: _env_bool("ENFORCE_QUOTAS", False))

    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    def public_view(self) -> dict:
        """Settings safe to show in the dashboard — secrets reduced to booleans."""
        return {
            "llm": {
                "provider": self.llm_provider,
                "base_url": self.llm_base_url,
                "model": self.llm_model,
                "timeout_s": self.llm_timeout_s,
                "history_turns": self.llm_history_turns,
                "api_key_configured": bool(self.llm_api_key),
            },
            "stt": {
                "provider": self.stt_provider,
                "model": self.stt_model,
                "multilingual_model": self.stt_multilingual_model,
                "base_url": self.stt_base_url,
                "api_key_configured": bool(self.stt_api_key),
            },
            "tts": {
                "provider": self.tts_provider,
                "model": self.tts_model,
                "base_url": self.tts_base_url,
                "api_key_configured": bool(self.tts_api_key),
                "gemini_model": self.gemini_tts_model,
                "gemini_key_configured": bool(self.gemini_api_key),
            },
            "audio": {
                "max_record_seconds": self.max_record_seconds,
                "sample_rate": self.sample_rate,
                "save_raw_audio": self.save_raw_audio,
            },
            "server": {
                "backend_url": self.backend_url,
                "heartbeat_timeout_s": self.heartbeat_timeout_s,
                "device_token_required": bool(self.device_token),
                "enforce_quotas": self.enforce_quotas,
            },
        }


settings = Settings()

# Non-secret fields the dashboard may change at runtime (kept in memory; .env stays the source of truth).
RUNTIME_EDITABLE = {
    "llm_provider", "llm_base_url", "llm_model", "llm_history_turns",
    "stt_provider", "stt_model", "stt_multilingual_model", "tts_provider", "tts_model",
    "max_record_seconds", "save_raw_audio",
}
