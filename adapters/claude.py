"""Claude Code history and hook adapter for verified Skill event shapes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .base import InstalledSkill, Invocation, canonical_path


_SLASH_COMMAND = re.compile(r"<command-name>\s*/([^<\s]+)\s*</command-name>")
_SKILL_INPUT_KEYS = ("skill", "name")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _agent_kind(payload: dict[str, Any], *, subagent: bool = False) -> str:
    if subagent or payload.get("agent_id") or payload.get("agentId"):
        return "subagent"
    return "main"


def _skill_name(tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    values = [tool_input.get(key) for key in _SKILL_INPUT_KEYS]
    names = [value.strip().lstrip("/") for value in values if isinstance(value, str) and value.strip()]
    if len(set(names)) != 1:
        return None
    return names[0] if names else None


def parse_post_tool_use(payload: dict[str, Any]) -> Invocation | None:
    """Parse one official PostToolUse payload without storing prompt or tool output."""
    if payload.get("hook_event_name") != "PostToolUse" or payload.get("tool_name") != "Skill":
        return None
    session_id = payload.get("session_id")
    tool_use_id = payload.get("tool_use_id")
    name = _skill_name(payload.get("tool_input"))
    if not all(isinstance(value, str) and value.strip() for value in (session_id, tool_use_id, name)):
        return None
    return Invocation(
        platform="claude",
        session_id=session_id,
        turn_id=tool_use_id,
        skill_name=name,
        skill_path=None,
        skill_key=f"name:{name.casefold()}",
        evidence_type="structured_skill",
        invoked_at=str(payload.get("timestamp") or _utc_now()),
        cwd=str(payload["cwd"]) if payload.get("cwd") else None,
        agent_kind=_agent_kind(payload),
        model=str(payload["model"]) if payload.get("model") else None,
        ingest_source="realtime",
    )


def parse_user_prompt_expansion(payload: dict[str, Any]) -> Invocation | None:
    """Parse one official slash-command expansion without retaining the original prompt."""
    if (
        payload.get("hook_event_name") != "UserPromptExpansion"
        or payload.get("expansion_type") != "slash_command"
    ):
        return None
    session_id = payload.get("session_id")
    command_name = payload.get("command_name")
    if not all(isinstance(value, str) and value.strip() for value in (session_id, command_name)):
        return None
    stable_id = next(
        (
            value.strip()
            for value in (payload.get("prompt_id"), payload.get("event_id"))
            if isinstance(value, str) and value.strip()
        ),
        None,
    )
    if stable_id is None:
        return None
    turn_id = stable_id
    name = command_name.strip().lstrip("/")
    return Invocation(
        platform="claude",
        session_id=session_id,
        turn_id=turn_id,
        skill_name=name,
        skill_path=None,
        skill_key=f"name:{name.casefold()}",
        evidence_type="slash_skill",
        invoked_at=str(payload.get("timestamp") or _utc_now()),
        cwd=str(payload["cwd"]) if payload.get("cwd") else None,
        agent_kind=_agent_kind(payload),
        model=str(payload["model"]) if payload.get("model") else None,
        ingest_source="realtime",
    )


def _version_key(path: Path) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.findall(r"\d+|[^\d]+", path.name)
    )


def _plugin_skills(cache: Path) -> Iterable[Path]:
    if not cache.is_dir():
        return
    for marketplace in sorted(path for path in cache.iterdir() if path.is_dir()):
        for plugin in sorted(path for path in marketplace.iterdir() if path.is_dir()):
            versions = [path for path in plugin.iterdir() if path.is_dir()]
            if versions:
                yield from max(versions, key=_version_key).rglob("SKILL.md")


def _fingerprint(path: Path, offset: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        remaining = offset
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


class ClaudeAdapter:
    platform = "claude"
    adapter_version = "1"
    format_version = "tested-jsonl-assistant-user-v1"

    def __init__(
        self,
        *,
        claude_home: str | os.PathLike[str] | None = None,
        project_dirs: Iterable[str | os.PathLike[str]] | None = None,
        skill_dirs: Iterable[str | os.PathLike[str]] | None = None,
        db_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._root = Path(
            claude_home
            or os.environ.get("CLAUDE_CONFIG_DIR")
            or Path.home() / ".claude"
        ).expanduser()
        self._projects = [Path(path).expanduser() for path in (project_dirs or [Path.cwd()])]
        self._skill_dirs = [Path(path).expanduser() for path in (skill_dirs or [])]
        self._db_path = db_path

    @property
    def resolved_root(self) -> Path:
        return self._root.resolve(strict=False)

    @property
    def installed(self) -> bool:
        return self._root.is_dir()

    def discover_transcripts(self) -> list[Path]:
        projects = self._root / "projects"
        if not projects.is_dir():
            return []
        return sorted(
            projects.rglob("*.jsonl"),
            key=lambda path: (
                "subagents" in {part.casefold() for part in path.parts},
                str(path),
            ),
        )

    def _project_skill_roots(self) -> Iterable[Path]:
        seen: set[str] = set()
        user_home = Path.home().resolve(strict=False)
        for project in self._projects:
            current = project.resolve(strict=False)
            for directory in (current, *current.parents):
                if directory == user_home:
                    break
                root = directory / ".claude" / "skills"
                key = canonical_path(root)
                if key not in seen:
                    seen.add(key)
                    yield root

    def discover_installed_skills(self) -> list[InstalledSkill]:
        now = _utc_now()
        roots: list[tuple[Iterable[Path], str]] = []
        user_root = self._root / "skills"
        roots.append((user_root.rglob("SKILL.md") if user_root.is_dir() else (), "user"))
        for root in self._project_skill_roots():
            roots.append((root.rglob("SKILL.md") if root.is_dir() else (), "project"))
        for root in self._skill_dirs:
            roots.append((root.rglob("SKILL.md") if root.is_dir() else (), "configured"))
        roots.append((_plugin_skills(self._root / "plugins" / "cache"), "plugin"))

        discovered: dict[str, InstalledSkill] = {}
        for instructions, source in roots:
            for instruction in sorted(instructions):
                path = canonical_path(instruction)
                name, _ = self._skill_metadata(path)
                discovered[path] = InstalledSkill(
                    platform=self.platform,
                    skill_key=path,
                    skill_name=name,
                    skill_path=path,
                    skill_source=source,
                    first_seen_at=now,
                    last_seen_at=now,
                )
        return list(discovered.values())

    def discover_skills(self) -> list[InstalledSkill]:
        return self.discover_installed_skills()

    @staticmethod
    def _skill_metadata(skill_path: str) -> tuple[str, str]:
        import scanner

        return scanner._skill_metadata(skill_path)

    def _scan_transcript(
        self,
        transcript: Path,
        installed_by_name: dict[str, list[InstalledSkill]],
    ):
        import scanner

        path = transcript.resolve()
        source = str(path)
        stat = path.stat()
        connection = scanner.init_db(self._db_path or scanner.DEFAULT_DB_PATH)
        try:
            state = connection.execute(
                "SELECT byte_offset, file_size, file_mtime, cursor_fingerprint "
                "FROM scan_state WHERE platform = 'claude' AND source_path = ?",
                (source,),
            ).fetchone()
            offset = int(state[0]) if state else 0
            if state and (
                stat.st_size < offset
                or stat.st_size < int(state[1])
                or not state[3]
                or state[3] != _fingerprint(path, offset)
                or (
                    stat.st_size == int(state[1])
                    and stat.st_mtime_ns != int(state[2])
                    and offset == stat.st_size
                )
            ):
                offset = 0

            with path.open("rb") as stream:
                stream.seek(offset)
                new_data = stream.read()

            complete_length = lines = inserted = duplicates = upgraded = errors = 0
            subagent = "subagents" in {part.casefold() for part in path.parts}
            context: dict[str, str | None] = {
                "session_id": path.stem,
                "cwd": None,
                "model": None,
            }
            for raw_line in new_data.splitlines(keepends=True):
                if not raw_line.endswith((b"\n", b"\r")):
                    break
                line_offset = offset + complete_length
                complete_length += len(raw_line)
                lines += 1
                try:
                    entry = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    errors += 1
                    scanner._diagnose(
                        connection, source, line_offset, lines, None, "invalid_json",
                        "Claude JSONL line could not be decoded", platform=self.platform,
                        adapter_version=self.adapter_version, format_version=self.format_version,
                    )
                    continue
                if not isinstance(entry, dict):
                    errors += 1
                    scanner._diagnose(
                        connection, source, line_offset, lines, None, "unknown_entry",
                        "Claude JSONL entry is not an object", platform=self.platform,
                        adapter_version=self.adapter_version, format_version=self.format_version,
                    )
                    continue
                for key, target in (("sessionId", "session_id"), ("cwd", "cwd")):
                    if isinstance(entry.get(key), str) and entry[key]:
                        context[target] = entry[key]
                message = entry.get("message")
                entry_type = entry.get("type")
                if entry_type not in {"assistant", "user"} or not isinstance(message, dict):
                    errors += 1
                    scanner._diagnose(
                        connection, source, line_offset, lines, str(entry_type), "unknown_entry",
                        "Claude JSONL entry shape is not supported", platform=self.platform,
                        adapter_version=self.adapter_version, format_version=self.format_version,
                    )
                    continue
                if entry_type == "assistant" and isinstance(message.get("model"), str):
                    context["model"] = message["model"]
                turn_id = entry.get("uuid")
                if not isinstance(turn_id, str) or not turn_id:
                    errors += 1
                    scanner._diagnose(
                        connection, source, line_offset, lines, str(entry_type), "unknown_entry",
                        "Claude JSONL entry lacks a stable message UUID", platform=self.platform,
                        adapter_version=self.adapter_version, format_version=self.format_version,
                    )
                    continue
                invocations: list[Invocation] = []
                if entry_type == "assistant":
                    content = message.get("content")
                    if not isinstance(content, list):
                        errors += 1
                        scanner._diagnose(
                            connection, source, line_offset, lines, "assistant", "unknown_entry",
                            "Claude assistant content is not a list", platform=self.platform,
                            adapter_version=self.adapter_version, format_version=self.format_version,
                        )
                        continue
                    structured_names = {
                        name.casefold()
                        for block in content
                        if isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == "Skill"
                        and isinstance(block.get("id"), str)
                        and block["id"]
                        and (name := _skill_name(block.get("input")))
                    }
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        invocation = self._history_tool_invocation(
                            block, entry, context, turn_id, subagent
                        )
                        if invocation is None and block.get("name") == "Skill":
                            errors += 1
                            scanner._diagnose(
                                connection, source, line_offset, lines, "Skill",
                                "unsupported_skill_input",
                                "Skill tool input did not match a tested name field",
                                platform=self.platform, adapter_version=self.adapter_version,
                                format_version=self.format_version,
                            )
                        elif invocation is not None:
                            if (
                                invocation.evidence_type == "skill_file_read"
                                and invocation.skill_name.casefold() in structured_names
                            ):
                                continue
                            invocations.append(invocation)
                else:
                    content = message.get("content")
                    if isinstance(content, str):
                        match = _SLASH_COMMAND.search(content)
                        name = match.group(1) if match else None
                        if name and len(installed_by_name.get(name.casefold(), [])) == 1:
                            invocations.append(
                                self._invocation(entry, context, turn_id, name, None,
                                                 "slash_skill", subagent)
                            )

                for invocation in invocations:
                    matches = installed_by_name.get(invocation.skill_name.casefold(), [])
                    if invocation.skill_path is None and len(matches) == 1:
                        skill = matches[0]
                        invocation = replace(
                            invocation,
                            skill_name=skill.skill_name,
                            skill_path=skill.skill_path,
                            skill_key=skill.skill_key,
                        )
                    outcome = scanner.ingest_invocation(connection, invocation)
                    inserted += outcome == "inserted"
                    duplicates += outcome == "duplicate"
                    upgraded += outcome == "upgraded"

            new_offset = offset + complete_length
            connection.execute(
                """INSERT INTO scan_state
                       (platform, source_path, byte_offset, file_size, file_mtime,
                        cursor_fingerprint, updated_at)
                   VALUES ('claude', ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(platform, source_path) DO UPDATE SET
                       byte_offset = excluded.byte_offset,
                       file_size = excluded.file_size,
                       file_mtime = excluded.file_mtime,
                       cursor_fingerprint = excluded.cursor_fingerprint,
                       updated_at = excluded.updated_at""",
                (source, new_offset, stat.st_size, stat.st_mtime_ns,
                 _fingerprint(path, new_offset), _utc_now()),
            )
            connection.commit()
            return scanner.ScanResult(
                files=1, lines=lines, inserted=inserted, duplicates=duplicates,
                upgraded=upgraded, parse_errors=errors,
                retained_bytes=len(new_data) - complete_length,
            )
        finally:
            connection.close()

    def _history_tool_invocation(
        self,
        block: dict[str, Any],
        entry: dict[str, Any],
        context: dict[str, str | None],
        turn_id: str,
        subagent: bool,
    ) -> Invocation | None:
        tool_name = block.get("name")
        tool_input = block.get("input")
        if tool_name == "Skill":
            name = _skill_name(tool_input)
            tool_use_id = block.get("id")
            if not name or not isinstance(tool_use_id, str) or not tool_use_id:
                return None
            return self._invocation(
                entry, context, tool_use_id, name, None, "structured_skill", subagent
            )
        if tool_name != "Read" or not isinstance(tool_input, dict):
            return None
        value = tool_input.get("file_path")
        if not isinstance(value, str) or Path(value).name.casefold() != "skill.md":
            return None
        path = Path(value).expanduser()
        if not path.is_absolute() and context.get("cwd"):
            path = Path(str(context["cwd"])) / path
        normalized = canonical_path(path)
        name, _ = self._skill_metadata(normalized)
        return self._invocation(
            entry, context, turn_id, name, normalized, "skill_file_read", subagent
        )

    def _invocation(
        self,
        entry: dict[str, Any],
        context: dict[str, str | None],
        turn_id: str,
        name: str,
        path: str | None,
        evidence: str,
        subagent: bool,
    ) -> Invocation:
        return Invocation(
            platform=self.platform,
            session_id=str(context.get("session_id") or "unknown"),
            turn_id=turn_id,
            skill_name=name,
            skill_path=path,
            skill_key=path or f"name:{name.casefold()}",
            evidence_type=evidence,
            invoked_at=str(entry.get("timestamp") or _utc_now()),
            cwd=context.get("cwd"),
            agent_kind=_agent_kind(entry, subagent=subagent),
            model=context.get("model") if entry.get("type") == "assistant" else None,
            ingest_source="history",
        )

    def scan(self):
        import scanner

        db_path = self._db_path or scanner.DEFAULT_DB_PATH
        if not self.installed:
            scanner.update_platform_status(
                self.platform, "not_installed", db_path=db_path,
                resolved_root=self.resolved_root, adapter_version=self.adapter_version,
                format_version=self.format_version,
            )
            return scanner.ScanResult()

        installed = self.discover_installed_skills()
        scanner.replace_installed_skills(self.platform, installed, db_path, complete=False)
        by_name: dict[str, list[InstalledSkill]] = {}
        for skill in installed:
            by_name.setdefault(skill.skill_name.casefold(), []).append(skill)

        result = scanner.ScanResult()
        for transcript in self.discover_transcripts():
            try:
                result += self._scan_transcript(transcript, by_name)
            except (OSError, UnicodeError, sqlite3.Error, ValueError) as error:
                connection = scanner.init_db(db_path)
                try:
                    scanner._diagnose(
                        connection, str(transcript.resolve(strict=False)), 0, 0, None,
                        "scan_failure", type(error).__name__, platform=self.platform,
                        adapter_version=self.adapter_version, format_version=self.format_version,
                    )
                    connection.commit()
                finally:
                    connection.close()
                result += scanner.ScanResult(files=1, failures=1)

        partial = bool(result.failures or result.parse_errors)
        scanner.replace_installed_skills(
            self.platform, installed, db_path, complete=not partial
        )
        scanner.update_platform_status(
            self.platform, "partial" if partial else "ready", db_path=db_path,
            resolved_root=self.resolved_root, last_history_scan_at=_utc_now(),
            adapter_version=self.adapter_version, format_version=self.format_version,
        )
        return result
