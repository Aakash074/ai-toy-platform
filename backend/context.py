"""Application context: builds and owns every service. Created once at startup."""
from __future__ import annotations

import logging

from backend.ai.llm.providers import build_llm
from backend.ai.stt.providers import build_stt
from backend.ai.tts.providers import build_tts
from backend.ai.tts.voice_cache import CachedTTS, VoiceCache, VoiceLines
from backend.config import Settings
from backend.devices.registry import DeviceRegistry
from backend.personalities.registry import PersonalityRegistry
from backend.services.conversation import ConversationService
from backend.services.events import DeviceHub, EventBus
from backend.services.usage import UsageService
from backend.storage.db import Database

log = logging.getLogger("context")


class AppContext:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.bus = EventBus()
        self.hub = DeviceHub()
        self.devices = DeviceRegistry(self.db, settings.heartbeat_timeout_s)
        self.personalities = PersonalityRegistry(settings.personalities_path)
        self.usage = UsageService(self.db)
        self.voice_cache = VoiceCache(settings.db_path.parent / "voice_cache")
        self.voice_lines = VoiceLines(settings.personalities_path.parent / "voice_lines.yaml")
        self.conversation = ConversationService(
            db=self.db, devices=self.devices, personalities=self.personalities, usage=self.usage,
            bus=self.bus, hub=self.hub, settings=settings,
            stt=build_stt(settings), llm=build_llm(settings), tts=CachedTTS(build_tts(settings), self.voice_cache),
            voice_lines=self.voice_lines,
        )
        self._ensure_default_virtual_device()
        log.info("Providers: STT=%s LLM=%s(%s) TTS=%s", settings.stt_provider, settings.llm_provider,
                 settings.llm_model, settings.tts_provider)

    def _ensure_default_virtual_device(self) -> None:
        if not self.devices.list():
            self.devices.register("virtual-toy-01", name="Virtual Teddy (no hardware)", is_virtual=True,
                                  personality_id=self.personalities.default_id())

    def rebuild_providers(self) -> None:
        """Re-create adapters after a runtime settings change. Raises ValueError on bad config."""
        stt, llm, tts = build_stt(self.settings), build_llm(self.settings), build_tts(self.settings)
        c = self.conversation
        c.stt, c.llm, c.tts = stt, llm, CachedTTS(tts, self.voice_cache)
        log.info("Providers rebuilt: STT=%s LLM=%s(%s) TTS=%s", stt.name, llm.name, self.settings.llm_model, tts.name)
