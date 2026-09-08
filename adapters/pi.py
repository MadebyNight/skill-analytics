"""Pi Session JSONL v3 adapter using successful ``read SKILL.md`` evidence."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .base import InstalledSkill, Invocation, canonical_path


_KNOWN_ENTRY_TYPES = {
    "message",
    "model_change",
    "thinking_level_change",
    "compaction",
    "branch_summary",
    "custom",
    "custom_message",
    "label",
    "session_info",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_realtime_event(payload: object) -> Invocation | None:
    """Normalize one successful Pi Extension event without retaining tool output."""
    if not isinstance(payload, dict) or payload.get("toolName") != "read":
        return None
    session_id = payload.get("sessionId")
    call_id = payload.get("toolCallId")
    arguments = payload.get("args")
    path_value = arguments.get("path") if isinstance(arguments, dict) else None
    if not all(
        isinstance(value, str) and value.strip()
        for value in (session_id, call_id, path_value)
    ):
        return None
    path = Path(path_value.strip().removeprefix("@"))
    if path.name.casefold() != "skill.md":
        return None
    cwd = payload.get("cwd")
    if not path.is_absolute() and isinstance(cwd, str) and cwd:
        path = Path(cwd) / path
    resolved_path = canonical_path(path)
    import scanner

    name, _ = scanner._skill_metadata(resolved_path)
    provider = payload.get("provider")
    model_id = payload.get("model")
    if isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
        model = f"{provider}/{model_id}"
    elif isinstance(model_id, str) and model_id:
        model = model_id
    else:
        model = None
    timestamp = payload.get("timestamp")
    return Invocation(
        platform="pi",
        session_id=session_id.strip(),
        turn_id=call_id.strip(),
        skill_name=name,
        skill_path=resolved_path,
        skill_key=resolved_path,
        evidence_type="skill_file_read",
        invoked_at=timestamp if isinstance(timestamp, str) and timestamp else _utc_now(),
        cwd=canonical_path(cwd) if isinstance(cwd, str) and cwd else None,
        agent_kind="main",
        model=model,
        ingest_source="realtime",
    )


class PiAdapter:
    platform = "pi"
    adapter_version = "1"
    format_version = "session-jsonl-v3"

    def __init__(
        self,
        *,
        pi_home: str | os.PathLike[str] | None = None,
        session_dir: str | os.PathLike[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        home: str | os.PathLike[str] | None = None,
        skill_dirs: Iterable[str | os.PathLike[str]] | None = None,
        package_dirs: Iterable[str | os.PathLike[str]] | None = None,
        db_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._home = Path(home or Path.home()).expanduser()
        self._root = self._path(
            pi_home
            or os.environ.get("PI_CODING_AGENT_DIR")
            or self._home / ".pi" / "agent"
        )
        self._cwd = self._path(cwd or Path.cwd())
        self._skill_dirs = [self._path(path) for path in (skill_dirs or ())]
        self._package_dirs = [self._path(path) for path in (package_dirs or ())]
        self._db_path = db_path
        self._session_dir = self._resolve_session_dir(session_dir)

    def _path(
        self, value: str | os.PathLike[str], *, base: Path | None = None
    ) -> Path:
        text = os.fspath(value)
        if text == "~" or text.startswith(("~/", "~\\")):
            text = str(self._home) + text[1:]
        path = Path(text)
        return path if path.is_absolute() else (base or Path.cwd()) / path

    @property
    def resolved_root(self) -> Path:
        return self._root.resolve(strict=False)

    @property
    def resolved_session_dir(self) -> Path:
        return self._session_dir.resolve(strict=False)

    @property
    def installed(self) -> bool:
        return self._root.is_dir()

    def _settings(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _resolve_session_dir(
        self, explicit: str | os.PathLike[str] | None
    ) -> Path:
        if explicit is not None:
            return self._path(explicit)
        environment = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        if environment:
            return self._path(environment)
        configured = self._settings(self._root / "settings.json").get("sessionDir")
        if isinstance(configured, str) and configured.strip():
            return self._path(configured.strip(), base=self._cwd)
        return self._root / "sessions"

    def discover_transcripts(self) -> list[Path]:
        if not self._session_dir.is_dir():
            return []
        return sorted(self._session_dir.rglob("*.jsonl"))

    def _project_ancestors(self) -> Iterable[Path]:
        current = self._cwd.resolve(strict=False)
        ancestors = (current, *current.parents)
        worktree = next((path for path in ancestors if (path / ".git").exists()), None)
        for path in ancestors:
            yield path
            if path == worktree:
                break

    def _configured_paths(self, settings_path: Path, key: str) -> list[Path]:
        values = self._settings(settings_path).get(key)
        if key == "skills" and isinstance(values, dict):
            values = values.get("customDirectories")
        if not isinstance(values, list):
            return []
        result: list[Path] = []
        for item in values:
            value = item.get("source") if key == "packages" and isinstance(item, dict) else item
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            if value.startswith(("!", "-")):
                continue
            value = value.removeprefix("+")
            if key == "packages" and (
                value.startswith("npm:")
                or not value.startswith((".", "~", "/", "\\"))
                and not Path(value).is_absolute()
                and not value.startswith(("git:", "github:", "http:", "https:", "ssh:"))
            ):
                specification = value.removeprefix("npm:")
                if specification.startswith("@"):
                    separator = specification.find("@", specification.find("/") + 1)
                else:
                    separator = specification.find("@")
                name = specification if separator < 0 else specification[:separator]
                scope_root = self._root if settings_path == self._root / "settings.json" else settings_path.parent
                result.append(scope_root / "npm" / "node_modules" / Path(*name.split("/")))
                continue
            if value.startswith(("git:", "github:", "http:", "https:", "ssh:")):
                continue
            candidate = self._path(value, base=settings_path.parent)
            if glob.has_magic(str(candidate)):
                result.extend(Path(match) for match in glob.glob(str(candidate), recursive=True))
            else:
                result.append(candidate)
        return result

    @staticmethod
    def _declares_skill(path: Path) -> bool:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return False
        if not lines or lines[0].strip() != "---":
            return False
        for line in lines[1:80]:
            if line.strip() == "---":
                break
            key, separator, value = line.partition(":")
            if separator and key.strip().casefold() == "description" and value.strip():
                return True
        return False

    @classmethod
    def _instructions(cls, root: Path, *, agents_root: bool = False) -> Iterable[Path]:
        if root.is_file():
            if root.name.casefold() == "skill.md" or (
                root.suffix.casefold() == ".md" and cls._declares_skill(root)
            ):
                yield root
            return
        if root.is_dir():
            yield from sorted(root.rglob("SKILL.md"))
            yield from sorted(
                path
                for path in (root.rglob("*.md") if agents_root else root.glob("*.md"))
                if path.name.casefold() != "skill.md"
                and (not agents_root or path.parent != root)
                and cls._declares_skill(path)
            )

    def _package_skill_roots(self, package: Path) -> Iterable[Path]:
        if package.is_file():
            package = package.parent
        manifest = self._settings(package / "package.json")
        pi = manifest.get("pi") if isinstance(manifest.get("pi"), dict) else {}
        configured = pi.get("skills")
        if isinstance(configured, list):
            for value in configured:
                if isinstance(value, str) and value and not value.startswith(("!", "-")):
                    candidate = self._path(value.removeprefix("+"), base=package)
                    if glob.has_magic(str(candidate)):
                        yield from (Path(match) for match in glob.glob(str(candidate), recursive=True))
                    else:
                        yield candidate
        else:
            yield package / "skills"

    def discover_installed_skills(self) -> list[InstalledSkill]:
        roots: list[tuple[Path, str]] = [
            (self._root / "skills", "pi"),
            (self._home / ".agents" / "skills", "agents"),
        ]
        settings_files = [self._root / "settings.json"]
        for ancestor in self._project_ancestors():
            roots.extend(
                (
                    (ancestor / ".pi" / "skills", "project"),
                    (ancestor / ".agents" / "skills", "agents"),
                )
            )
            settings_files.append(ancestor / ".pi" / "settings.json")
        for settings in settings_files:
            roots.extend((path, "configured") for path in self._configured_paths(settings, "skills"))
        roots.extend((path, "explicit") for path in self._skill_dirs)

        packages = list(self._package_dirs)
        for settings in settings_files:
            packages.extend(self._configured_paths(settings, "packages"))
        for package in packages:
            roots.extend((path, "package") for path in self._package_skill_roots(package))

        now = _utc_now()
        discovered: dict[str, InstalledSkill] = {}
        for root, source in roots:
            for instruction in self._instructions(root, agents_root=source == "agents"):
                path = canonical_path(instruction)
                name, _ = self._skill_metadata(path)
                discovered.setdefault(
                    path,
                    InstalledSkill(
                        platform=self.platform,
                        skill_key=path,
                        skill_name=name,
                        skill_path=path,
                        skill_source=source,
                        first_seen_at=now,
                        last_seen_at=now,
                    ),
                )
        return list(discovered.values())

    def discover_skills(self) -> list[InstalledSkill]:
        return self.discover_installed_skills()

    @staticmethod
    def _skill_metadata(skill_path: str) -> tuple[str, str]:
        import scanner

        return scanner._skill_metadata(skill_path)

    @staticmethod
    def _is_descendant(
        result_entry: dict[str, Any], assistant_id: str, entries: dict[str, dict[str, Any]]
    ) -> bool:
        parent = result_entry.get("parentId")
        visited: set[str] = set()
        while isinstance(parent, str) and parent and parent not in visited:
            if parent == assistant_id:
                return True
            visited.add(parent)
            entry = entries.get(parent)
            parent = entry.get("parentId") if entry else None
        return False

    def _skill_path(self, value: Any, cwd: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip().removeprefix("@")
        if Path(text).name.casefold() != "skill.md":
            return None
        return canonical_path(self._path(text, base=Path(cwd)))

    def _parse_transcript(
        self,
        transcript: Path,
        *,
        new_line_start: int = 0,
        byte_limit: int | None = None,
    ) -> tuple[list[Invocation], int, list[tuple[int, str, str]]]:
        data = transcript.read_bytes()
        raw_lines = data[:byte_limit].splitlines() if byte_limit is not None else data.splitlines()
        parsed: list[tuple[int, dict[str, Any]]] = []
        diagnostics: list[tuple[int, str, str]] = []
        for number, raw in enumerate(raw_lines, 1):
            try:
                entry = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                diagnostics.append((number, "invalid_json", "Pi JSONL line could not be decoded"))
                continue
            if not isinstance(entry, dict):
                diagnostics.append((number, "unknown_entry", "Pi JSONL entry is not an object"))
                continue
            parsed.append((number, entry))

        if not parsed or parsed[0][1].get("type") != "session":
            diagnostics.append((1, "unknown_entry", "Pi session header is missing"))
            return [], len(raw_lines), diagnostics
        header = parsed[0][1]
        version = header.get("version")
        if version not in (None, 3):
            diagnostics.append((parsed[0][0], "unsupported_version", f"Pi session version {version!r} is not supported"))
            return [], len(raw_lines), diagnostics
        session_id = header.get("id")
        cwd = header.get("cwd")
        if not isinstance(session_id, str) or not session_id or not isinstance(cwd, str) or not cwd:
            diagnostics.append((parsed[0][0], "unknown_entry", "Pi session header lacks id or cwd"))
            return [], len(raw_lines), diagnostics

        entries: dict[str, dict[str, Any]] = {}
        assistants: list[tuple[int, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        results: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for number, entry in parsed[1:]:
            entry_type = entry.get("type")
            if entry_type not in _KNOWN_ENTRY_TYPES:
                diagnostics.append((number, "unknown_entry", f"unsupported Pi entry type {entry_type!r}"))
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str) and entry_id:
                entries[entry_id] = entry
            if entry_type != "message":
                continue
            message = entry.get("message")
            if not isinstance(entry_id, str) or not entry_id or not isinstance(message, dict):
                diagnostics.append((number, "unknown_entry", "Pi message entry lacks id or message"))
                continue
            role = message.get("role")
            if role == "assistant":
                content = message.get("content")
                if not isinstance(content, list):
                    diagnostics.append((number, "unknown_entry", "Pi assistant content is not a list"))
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        assistants.append((number, entry, message, block))
            elif role == "toolResult":
                results.append((number, entry, message))

        invocations: list[Invocation] = []
        for assistant_line, entry, message, block in assistants:
            call_id = block.get("id")
            arguments = block.get("arguments")
            if block.get("name") != "read" or not isinstance(call_id, str) or not call_id:
                continue
            if not isinstance(arguments, dict):
                continue
            skill_path = self._skill_path(arguments.get("path"), cwd)
            if skill_path is None:
                continue
            matching_results = [
                (result_line, result_entry, result)
                for result_line, result_entry, result in results
                if (
                    result.get("toolCallId") == call_id
                    and result.get("toolName") == "read"
                    and result.get("isError") is False
                    and self._is_descendant(result_entry, str(entry["id"]), entries)
                )
            ]
            if not matching_results or not (
                assistant_line > new_line_start
                or any(line > new_line_start for line, _, _ in matching_results)
            ):
                continue
            skill_name, _ = self._skill_metadata(skill_path)
            provider = message.get("provider")
            model_id = message.get("model")
            if isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
                model = f"{provider}/{model_id}"
            elif isinstance(model_id, str) and model_id:
                model = model_id
            else:
                model = None
            invocations.append(
                Invocation(
                    platform=self.platform,
                    session_id=session_id,
                    turn_id=call_id,
                    skill_name=skill_name,
                    skill_path=skill_path,
                    skill_key=skill_path,
                    evidence_type="skill_file_read",
                    invoked_at=str(
                        entry.get("timestamp") or header.get("timestamp") or _utc_now()
                    ),
                    cwd=canonical_path(cwd),
                    agent_kind="main",
                    model=model,
                    ingest_source="history",
                )
            )
        return invocations, len(raw_lines), diagnostics

    @staticmethod
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

    def _diagnose(
        self,
        transcript: Path,
        diagnostics: Iterable[tuple[int, str, str]],
    ) -> None:
        import scanner

        connection = scanner.init_db(self._db_path or scanner.DEFAULT_DB_PATH)
        try:
            for line, category, detail in diagnostics:
                scanner._diagnose(
                    connection,
                    str(transcript.resolve()),
                    0,
                    line,
                    None,
                    category,
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

        installed = self.discover_installed_skills()
        scanner.replace_installed_skills(self.platform, installed, db_path, complete=True)
        result = scanner.ScanResult()
        diagnostic_categories: set[str] = set()
        for transcript in self.discover_transcripts():
            try:
                source = str(transcript.resolve())
                stat = transcript.stat()
                connection = scanner.init_db(db_path)
                try:
                    state = connection.execute(
                        "SELECT byte_offset, file_size, file_mtime, cursor_fingerprint "
                        "FROM scan_state WHERE platform='pi' AND source_path=?",
                        (source,),
                    ).fetchone()
                    previous_categories = {
                        row[0]
                        for row in connection.execute(
                            "SELECT category FROM diagnostics "
                            "WHERE platform='pi' AND source_path=?",
                            (source,),
                        )
                    }
                finally:
                    connection.close()
                offset = int(state[0]) if state else 0
                reset = bool(state and (
                    stat.st_size < offset
                    or stat.st_size < int(state[1])
                    or not state[3]
                    or state[3] != self._fingerprint(transcript, offset)
                    or (
                        stat.st_size == int(state[1])
                        and stat.st_mtime_ns != int(state[2])
                        and offset == stat.st_size
                    )
                ))
                if reset:
                    offset = 0
                if offset == stat.st_size:
                    diagnostic_categories.update(previous_categories)
                    continue
                with transcript.open("rb") as stream:
                    prefix = stream.read(offset)
                    new_data = stream.read()
                complete_length = 0
                for raw in new_data.splitlines(keepends=True):
                    if not raw.endswith((b"\n", b"\r")):
                        break
                    complete_length += len(raw)
                if complete_length == 0:
                    diagnostic_categories.update(previous_categories)
                    result += scanner.ScanResult(files=1, retained_bytes=len(new_data))
                    continue
                complete_offset = offset + complete_length
                prefix_lines = prefix.count(b"\n")
                invocations, _, diagnostics = self._parse_transcript(
                    transcript,
                    new_line_start=prefix_lines,
                    byte_limit=complete_offset,
                )
                if offset:
                    diagnostics = [item for item in diagnostics if item[0] > prefix_lines]
                    diagnostic_categories.update(previous_categories)
                diagnostic_categories.update(category for _, category, _ in diagnostics)
                stored = scanner.store_invocations(invocations, db_path)
                self._diagnose(transcript, diagnostics)
                connection = scanner.init_db(db_path)
                try:
                    connection.execute(
                        """INSERT INTO scan_state
                               (platform, source_path, byte_offset, file_size, file_mtime,
                                cursor_fingerprint, updated_at)
                           VALUES ('pi', ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(platform, source_path) DO UPDATE SET
                               byte_offset=excluded.byte_offset,
                               file_size=excluded.file_size,
                               file_mtime=excluded.file_mtime,
                               cursor_fingerprint=excluded.cursor_fingerprint,
                               updated_at=excluded.updated_at""",
                        (
                            source,
                            complete_offset,
                            stat.st_size,
                            stat.st_mtime_ns,
                            self._fingerprint(transcript, complete_offset),
                            _utc_now(),
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()
                result += scanner.ScanResult(
                    files=1,
                    lines=len(new_data[:complete_length].splitlines()),
                    inserted=stored.inserted,
                    duplicates=stored.duplicates,
                    upgraded=stored.upgraded,
                    parse_errors=len(diagnostics),
                    retained_bytes=len(new_data) - complete_length,
                )
            except (OSError, UnicodeError, sqlite3.Error, ValueError) as error:
                self._diagnose(transcript, [(0, "scan_failure", type(error).__name__)])
                diagnostic_categories.add("scan_failure")
                result += scanner.ScanResult(files=1, failures=1)

        if diagnostic_categories == {"unsupported_version"} and not result.failures:
            status = "unsupported_version"
        elif diagnostic_categories or result.failures:
            status = "partial"
        else:
            status = "ready"
        scanner.update_platform_status(
            self.platform,
            status,
            db_path=db_path,
            resolved_root=self.resolved_root,
            last_history_scan_at=_utc_now(),
            adapter_version=self.adapter_version,
            format_version=self.format_version,
        )
        return result
