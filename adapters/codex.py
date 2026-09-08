"""Codex compatibility adapter built on the verified legacy scanner."""

from __future__ import annotations

import os
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .base import InstalledSkill, canonical_path


def _version_key(path: Path) -> tuple[tuple[int, int | str], ...]:
    version = path.name
    manifest = path / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("version"):
            version = str(payload["version"])
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.findall(r"\d+|[^\d]+", version)
    )


def _current_plugin_skills(cache: Path):
    if not cache.is_dir():
        return
    for marketplace in sorted(path for path in cache.iterdir() if path.is_dir()):
        for plugin in sorted(path for path in marketplace.iterdir() if path.is_dir()):
            if plugin.name.casefold().startswith("plugin-backup-"):
                continue
            latest = plugin / "latest"
            if latest.is_dir():
                current = latest.resolve(strict=False)
            else:
                versions = [path for path in plugin.iterdir() if path.is_dir()]
                if not versions:
                    continue
                current = max(versions, key=_version_key)
            yield from current.rglob("SKILL.md")


class CodexAdapter:
    platform = "codex"
    adapter_version = "1"
    format_version = "response_item/custom_tool_call/exec"

    def __init__(
        self,
        *,
        codex_home: str | os.PathLike[str] | None = None,
        agents_home: str | os.PathLike[str] | None = None,
        plugin_root: str | os.PathLike[str] | None = None,
        db_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._root = Path(
            codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        ).expanduser()
        self._agents_home = Path(agents_home or Path.home() / ".agents").expanduser()
        self._plugin_root = Path(
            plugin_root or self._root / "plugins" / "cache"
        ).expanduser()
        self._db_path = db_path

    @property
    def resolved_root(self) -> Path:
        return self._root.resolve(strict=False)

    @property
    def installed(self) -> bool:
        return self._root.is_dir()

    def discover_transcripts(self) -> list[Path]:
        sessions = self._root / "sessions"
        return sorted(sessions.rglob("*.jsonl")) if sessions.is_dir() else []

    def discover_installed_skills(self) -> list[InstalledSkill]:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        codex_skills = self._root / "skills"
        system_skills = codex_skills / ".system"
        roots = (
            (codex_skills.rglob("SKILL.md") if codex_skills.is_dir() else (), "codex"),
            (
                (self._agents_home / "skills").rglob("SKILL.md")
                if (self._agents_home / "skills").is_dir()
                else (),
                "agents",
            ),
            (_current_plugin_skills(self._plugin_root), "plugin"),
        )
        discovered: dict[str, InstalledSkill] = {}
        for instructions, source in roots:
            for instruction in sorted(instructions):
                path = canonical_path(instruction)
                name, _ = self._skill_metadata(path)
                resolved = Path(path)
                detected_source = (
                    "system"
                    if source == "codex" and system_skills.resolve(strict=False) in resolved.parents
                    else "other"
                    if source == "codex"
                    else source
                )
                discovered[path] = InstalledSkill(
                    platform=self.platform,
                    skill_key=path,
                    skill_name=name,
                    skill_path=path,
                    skill_source=detected_source if source == "codex" else source,
                    first_seen_at=now,
                    last_seen_at=now,
                )
        return list(discovered.values())

    def discover_skills(self) -> list[InstalledSkill]:
        """Compatibility alias for callers using the shorter adapter method name."""
        return self.discover_installed_skills()

    def scan(self):
        import scanner

        kwargs = {"codex_home": self._root}
        if self._db_path is not None:
            kwargs["db_path"] = self._db_path
        db_path = self._db_path or scanner.DEFAULT_DB_PATH
        if not self.installed:
            scanner.update_platform_status(
                self.platform,
                "not_installed",
                db_path=db_path,
                resolved_root=self.resolved_root,
                adapter_version=self.adapter_version,
                format_version=self.format_version,
            )
            return scanner.ScanResult()
        installed_skills = self.discover_installed_skills()
        scanner.replace_installed_skills(
            self.platform, installed_skills, db_path, complete=False
        )
        result = scanner.backfill(**kwargs)
        scanner.replace_installed_skills(
            self.platform,
            installed_skills,
            db_path,
            complete=not (result.failures or result.parse_errors),
        )
        scanner.update_platform_status(
            self.platform,
            "partial" if result.failures or result.parse_errors else "ready",
            db_path=db_path,
            resolved_root=self.resolved_root,
            last_history_scan_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            adapter_version=self.adapter_version,
            format_version=self.format_version,
        )
        return result

    @staticmethod
    def _skill_metadata(skill_path: str) -> tuple[str, str]:
        import scanner

        return scanner._skill_metadata(skill_path)
