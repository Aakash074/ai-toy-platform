"""Provider-agnostic interfaces for the AI pipeline.

Every provider implements one of these small async interfaces, so swapping a model
(cloud ↔ local) is a config change, not a code change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProviderError(Exception):
    """Raised by any adapter on failure. `stage` is stt|llm|tts, `retryable` hints the caller."""

    def __init__(self, stage: str, message: str, retryable: bool = False):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
        self.message = message
        self.retryable = retryable


@dataclass
class Transcript:
    text: str
    language: str | None = None
    audio_seconds: float = 0.0


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    finish_reason: str = ""


@dataclass
class SpeechAudio:
    data: bytes
    mime: str = "audio/wav"
    meta: dict = field(default_factory=dict)


class STTProvider(ABC):
    name = "base"

    @abstractmethod
    async def transcribe(self, wav_bytes: bytes, language: str | None = None) -> Transcript: ...

    async def warmup(self, language: str | None = None) -> None:  # optional; may preload per language
        return None


class LLMProvider(ABC):
    name = "base"

    @abstractmethod
    async def chat(self, system: str, messages: list[ChatMessage], max_tokens: int) -> LLMResult: ...


class TTSProvider(ABC):
    name = "base"

    @abstractmethod
    async def synthesize(self, text: str, voice: dict) -> SpeechAudio: ...
