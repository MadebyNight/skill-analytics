"""Shared records and the minimal contract implemented by platform adapters."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


PLATFORMS = ("codex", "claude", "opencode", "pi")
EVIDENCE_PRIORITY = {
    "skill_file_read": 1,
    "slash_skill": 2,
    "structured_skill": 3,
}


def canonical_path(value: str | os.PathLike[str]) -> str:
    """Return the stable platform-local identity used for path-backed Skills."""
    return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))


@dataclass(frozen=True)
class Invocation:
    platform: str
    session_id: str
    turn_id: str
    skill_name: str
    skill_path: str | None
    skill_key: str
    evidence_type: str
    invoked_at: str
    cwd: str | None
    agent_kind: str
    model: str | None
    ingest_source: str


@dataclass(frozen=True)
class InstalledSkill:
    platform: str
    skill_key: str
    skill_name: str
    skill_path: str | None
    skill_source: str
    first_seen_at: str
    last_seen_at: str


class Adapter(Protocol):
    platform: str

    @property
    def resolved_root(self) -> Path: ...

    def discover_installed_skills(self) -> list[InstalledSkill]: ...

    def scan(self) -> Any: ...
