"""OpenCode adapter using only the documented Skill paths and public CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import InstalledSkill, Invocation, canonical_path


_KNOWN_PART_TYPES = {
    "agent",
    "compaction",
    "file",
    "patch",
    "reasoning",
    "retry",
    "snapshot",
    "step-finish",
    "step-start",
    "subtask",
    "text",
    "tool",
}
_KNOWN_TOOL_STATES = {"pending", "running", "completed", "error"}


@dataclass(frozen=True)
class _ExportParseResult:
    invocations: list[Invocation]
    recognized: bool
    unsupported: int


def _resolve_command(command: str | os.PathLike[str]) -> str:
    value = os.fspath(command)
    if os.name != "nt" or "/" in value or "\\" in value:
        return value
    return shutil.which(value) or value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str) and value:
        return value
    return _utc_now()


def extract_invocations(
    payload: object, *, session_id: str | None = None
) -> list[Invocation]:
    """Extract successful official ``tool`` parts from one CLI export payload."""
    return _parse_export(payload, session_id=session_id).invocations


def _parse_export(
    payload: object, *, session_id: str | None = None
) -> _ExportParseResult:
    if not isinstance(payload, dict):
        return _ExportParseResult([], False, 1)
    session = payload.get("info")
    messages = payload.get("messages")
    if not isinstance(session, dict) or not isinstance(messages, list):
        return _ExportParseResult([], False, 1)
    resolved_session_id = session_id or session.get("id")
    if not isinstance(resolved_session_id, str) or not resolved_session_id:
        return _ExportParseResult([], False, 1)

    session_time = session.get("time")
    session_time = session_time if isinstance(session_time, dict) else {}
    session_created = session_time.get("created")
    directory = session.get("directory")
    session_cwd = directory if isinstance(directory, str) and directory else None
    agent_kind = "subagent" if session.get("parentID") else "main"
    result: list[Invocation] = []
    unsupported = 0
    for message in messages:
        if not isinstance(message, dict):
            unsupported += 1
            continue
        info = message.get("info")
        parts = message.get("parts")
        if not isinstance(info, dict) or not isinstance(parts, list):
            unsupported += 1
            continue
        message_time = info.get("time")
        message_time = message_time if isinstance(message_time, dict) else {}
        path = info.get("path")
        path = path if isinstance(path, dict) else {}
        cwd_value = path.get("cwd")
        cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else session_cwd
        provider = info.get("providerID")
        model_id = info.get("modelID")
        if isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
            model = f"{provider}/{model_id}"
        elif isinstance(model_id, str) and model_id:
            model = model_id
        else:
            model = None

        for part in parts:
            if not isinstance(part, dict):
                unsupported += 1
                continue
            part_type = part.get("type")
            if part_type not in _KNOWN_PART_TYPES:
                unsupported += 1
                continue
            if part_type != "tool":
                continue
            call_id = part.get("callID")
            tool = part.get("tool")
            state = part.get("state")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(tool, str)
                or not tool
                or not isinstance(state, dict)
                or state.get("status") not in _KNOWN_TOOL_STATES
                or not isinstance(state.get("input"), dict)
            ):
                unsupported += 1
                continue
            if tool != "skill":
                continue
            if state.get("status") != "completed":
                continue
            arguments = state.get("input")
            name = arguments.get("name") if isinstance(arguments, dict) else None
            if not isinstance(name, str) or not name.strip():
                unsupported += 1
                continue
            state_time = state.get("time")
            state_time = state_time if isinstance(state_time, dict) else {}
            invoked_at = _timestamp(
                state_time.get("start", message_time.get("created", session_created))
            )
            skill_name = name.strip()
            result.append(
                Invocation(
                    platform="opencode",
                    session_id=resolved_session_id,
                    turn_id=call_id,
                    skill_name=skill_name,
                    skill_path=None,
                    skill_key=f"name:{skill_name.casefold()}",
                    evidence_type="structured_skill",
                    invoked_at=invoked_at,
                    cwd=cwd,
                    agent_kind=agent_kind,
                    model=model,
                    ingest_source="history",
                )
            )
    return _ExportParseResult(result, True, unsupported)


class OpenCodeAdapter:
    platform = "opencode"
    adapter_version = "1"
    format_version = "cli-export/tool-part-v1"

    def __init__(
        self,
        *,
        config_dir: str | os.PathLike[str] | None = None,
        command: str | os.PathLike[str] = "opencode",
        cwd: str | os.PathLike[str] | None = None,
        home: str | os.PathLike[str] | None = None,
        db_path: str | os.PathLike[str] | None = None,
        timeout: float = 15,
    ) -> None:
        self._home = Path(home or Path.home()).expanduser()
        self._root = Path(
            config_dir
            or os.environ.get("OPENCODE_CONFIG_DIR")
            or self._home / ".config" / "opencode"
        ).expanduser()
        self._command = _resolve_command(command)
        self._cwd = Path(cwd or Path.cwd()).expanduser()
        self._db_path = db_path
        self._timeout = timeout

    @property
    def resolved_root(self) -> Path:
        return self._root.resolve(strict=False)

    def _project_ancestors(self):
        current = self._cwd.resolve(strict=False)
        worktree = next((path for path in (current, *current.parents) if (path / ".git").exists()), None)
        for path in (current, *current.parents):
            yield path
            if path == worktree:
                break

    @staticmethod
    def _skill_metadata(path: Path) -> str:
        name = path.parent.name
        try:
            with path.open("r", encoding="utf-8") as stream:
                if stream.readline().strip() != "---":
                    return name
                for line_number, line in enumerate(stream, start=2):
                    if line.strip() == "---" or line_number > 80:
                        break
                    key, separator, value = line.partition(":")
                    if separator and key.strip().casefold() == "name":
                        return value.strip().strip("\"'") or name
        except (OSError, UnicodeError):
            pass
        return name

    def discover_installed_skills(self) -> list[InstalledSkill]:
        roots: list[tuple[Path, str]] = [
            (self._root / "skills", "opencode"),
            (self._home / ".claude" / "skills", "claude"),
            (self._home / ".agents" / "skills", "agents"),
        ]
        for ancestor in self._project_ancestors():
            roots.extend(
                (
                    (ancestor / ".opencode" / "skills", "opencode"),
                    (ancestor / ".claude" / "skills", "claude"),
                    (ancestor / ".agents" / "skills", "agents"),
                )
            )

        now = _utc_now()
        discovered: dict[str, InstalledSkill] = {}
        for root, source in roots:
            if not root.is_dir():
                continue
            for instruction in sorted(root.glob("*/SKILL.md")):
                path = canonical_path(instruction)
                discovered[path] = InstalledSkill(
                    platform=self.platform,
                    skill_key=path,
                    skill_name=self._skill_metadata(instruction),
                    skill_path=path,
                    skill_source=source,
                    first_seen_at=now,
                    last_seen_at=now,
                )
        return list(discovered.values())

    def discover_skills(self) -> list[InstalledSkill]:
        return self.discover_installed_skills()

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["OPENCODE_CONFIG_DIR"] = str(self.resolved_root)
        return subprocess.run(
            [self._command, *arguments],
            capture_output=True,
            text=True,
            timeout=self._timeout,
            shell=False,
            cwd=self._cwd,
            env=environment,
        )

    def _update_status(self, status: str, *, scanned: bool) -> None:
        import scanner

        scanner.update_platform_status(
            self.platform,
            status,
            db_path=self._db_path or scanner.DEFAULT_DB_PATH,
            resolved_root=self.resolved_root,
            last_history_scan_at=_utc_now() if scanned else None,
            adapter_version=self.adapter_version,
            format_version=self.format_version,
        )

    def _diagnose_unsupported(self, session_id: str, unsupported: int, *, recognized: bool) -> None:
        import scanner

        connection = scanner.init_db(self._db_path or scanner.DEFAULT_DB_PATH)
        try:
            detail = (
                f"OpenCode export contained {unsupported} unsupported record(s)"
                if recognized
                else "OpenCode export schema is not supported"
            )
            scanner._diagnose(
                connection,
                f"opencode export {session_id}",
                0,
                0,
                "export",
                "unsupported_version",
                detail,
                platform=self.platform,
                adapter_version=self.adapter_version,
                format_version=self.format_version,
            )
            connection.commit()
        finally:
            connection.close()

    def scan(self):
        import scanner

        db_path = self._db_path or scanner.DEFAULT_DB_PATH
        installed_skills = self.discover_installed_skills()
        scanner.replace_installed_skills(
            self.platform, installed_skills, db_path, complete=True
        )
        try:
            listed = self._run(["session", "list", "--format", "json"])
        except FileNotFoundError:
            self._update_status("not_installed", scanned=False)
            return scanner.ScanResult()
        except (OSError, subprocess.SubprocessError):
            self._update_status("partial", scanned=True)
            return scanner.ScanResult(failures=1)

        if listed.returncode:
            self._update_status("partial", scanned=True)
            return scanner.ScanResult(failures=1)
        if not listed.stdout.strip():
            self._update_status("ready", scanned=True)
            return scanner.ScanResult()
        try:
            sessions = json.loads(listed.stdout)
        except json.JSONDecodeError:
            self._update_status("partial", scanned=True)
            return scanner.ScanResult(parse_errors=1)
        if not isinstance(sessions, list):
            self._update_status("partial", scanned=True)
            return scanner.ScanResult(parse_errors=1)

        result = scanner.ScanResult()
        recognized_export = False
        unsupported_export = False
        unsupported_errors = 0
        for session in sessions:
            session_id = session.get("id") if isinstance(session, dict) else None
            if not isinstance(session_id, str) or not session_id:
                result += scanner.ScanResult(parse_errors=1)
                continue
            try:
                exported = self._run(["export", session_id])
            except (OSError, subprocess.SubprocessError):
                result += scanner.ScanResult(files=1, failures=1)
                continue
            if exported.returncode:
                result += scanner.ScanResult(files=1, failures=1)
                continue
            if not exported.stdout.strip():
                result += scanner.ScanResult(files=1)
                continue
            try:
                payload: Any = json.loads(exported.stdout)
            except json.JSONDecodeError:
                result += scanner.ScanResult(files=1, parse_errors=1)
                continue
            parsed = _parse_export(payload, session_id=session_id)
            recognized_export = recognized_export or parsed.recognized
            if parsed.unsupported:
                unsupported_export = True
                unsupported_errors += parsed.unsupported
                self._diagnose_unsupported(
                    session_id, parsed.unsupported, recognized=parsed.recognized
                )
            stored = scanner.store_invocations(parsed.invocations, db_path)
            result += scanner.ScanResult(
                files=1,
                inserted=stored.inserted,
                duplicates=stored.duplicates,
                upgraded=stored.upgraded,
                parse_errors=parsed.unsupported,
            )

        if (
            unsupported_export
            and not recognized_export
            and not result.failures
            and result.parse_errors == unsupported_errors
        ):
            status = "unsupported_version"
        else:
            status = "partial" if result.failures or result.parse_errors else "ready"
        self._update_status(status, scanned=True)
        return result
