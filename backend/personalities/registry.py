"""Personality configuration loaded from YAML (config/personalities.yaml)."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("personalities")


class ResponseLength(BaseModel):
    max_sentences: int = Field(3, ge=1, le=10)
    max_tokens: int = Field(150, ge=16, le=1024)


class Personality(BaseModel):
    id: str = ""
    name: str
    system_prompt: str
    age_group: str = "3-8"
    language: str = "en"
    response_length: ResponseLength = ResponseLength()
    teaching_style: str = ""
    voice: dict = Field(default_factory=dict)


class PersonalityRegistry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items: dict[str, Personality] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            raw = yaml.safe_load(self.path.read_text()) or {}
            items = {}
            for pid, cfg in (raw.get("personalities") or {}).items():
                try:
                    items[pid] = Personality(id=pid, **cfg)
                except ValidationError as e:
                    log.error("Invalid personality '%s' skipped: %s", pid, e)
            if not items:
                raise RuntimeError(f"No valid personalities in {self.path}")
            self._items = items
            log.info("Loaded %d personalities: %s", len(items), ", ".join(items))

    def all(self) -> list[Personality]:
        with self._lock:
            return list(self._items.values())

    def get(self, pid: str) -> Personality | None:
        with self._lock:
            return self._items.get(pid)

    def default_id(self) -> str:
        with self._lock:
            return "teddy" if "teddy" in self._items else next(iter(self._items))

    def upsert(self, p: Personality) -> Personality:
        """Create/update and persist back to YAML."""
        with self._lock:
            self._items[p.id] = p
            self._save()
            return p

    def _save(self) -> None:
        data = {"personalities": {
            pid: p.model_dump(exclude={"id"}) for pid, p in self._items.items()
        }}
        header = ("# Toy personalities (managed by AI Toy Lab; safe to hand-edit).\n"
                  "# Safety rules are always prepended in code and cannot be overridden here.\n\n")
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
        tmp.replace(self.path)
