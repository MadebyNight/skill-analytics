"""Extract Skill reads from verified response_item/custom_tool_call/exec events."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "analytics.db"

_READ_COMMAND = re.compile(
    r"(?:^|[;&|\r\n]+)\s*(?:&\s*)?(?:command\s+)?(?:sudo\s+)?"
    r"(?:\$\w+\s*=\s*)?"
    r"(?P<verb>Get-Content|gc|cat|type|head|tail|sed|bat)\b"
    r"(?P<arguments>[^;&|\r\n]*)",
    re.IGNORECASE,
)
_CMD_JSON_STRING = re.compile(
    r'(?<![A-Za-z0-9_])(?:["\']?(?:cmd|command)["\']?)'
    r'\s*:\s*(?P<value>"(?:\\.|[^"\\])*")',
    re.IGNORECASE,
)
_WORKDIR_JSON_STRING = re.compile(
    r'(?<![A-Za-z0-9_])(?:["\']?workdir["\']?)'
    r'\s*:\s*(?P<value>"(?:\\.|[^"\\])*")',
    re.IGNORECASE,
)
_DYNAMIC_ENUMERATION = re.compile(
    r"\$\(\s*(?:Get-ChildItem|gci|rg\s+--files|find|fd)\b"
    r"|\(\s*(?:Get-ChildItem|gci)\b|`",
    re.IGNORECASE,
)
_FRONTMATTER_NAME = re.compile(r"^name\s*:\s*(.+?)\s*$", re.IGNORECASE)
_PS_ASSIGNMENT = re.compile(
    r"(?:^|[;\r\n]+)\s*\$(?P<name>\w+)\s*=\s*(?P<value>[^;\r\n]*)",
    re.IGNORECASE,
)
_QUOTED_VALUE = re.compile(r"^(?P<quote>['\"])(?P<value>.*)(?P=quote)$")
_PS_VARIABLE = re.compile(r"(?<![A-Za-z0-9_$])\$(?P<name>\w+)\b", re.IGNORECASE)
_ARGUMENT_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'[^\']*\'|>>?|[^\s>]+')


@dataclass(frozen=True)
class ScanResult:
    files: int = 0
    lines: int = 0
    inserted: int = 0
    duplicates: int = 0
    parse_errors: int = 0
    retained_bytes: int = 0

    def __add__(self, other: "ScanResult") -> "ScanResult":
        return ScanResult(
            files=self.files + other.files,
            lines=self.lines + other.lines,
            inserted=self.inserted + other.inserted,
            duplicates=self.duplicates + other.duplicates,
            parse_errors=self.parse_errors + other.parse_errors,
            retained_bytes=self.retained_bytes + other.retained_bytes,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def init_db(db_path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create the analytics schema and return an open SQLite connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=2.0)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS skills (
            skill_path TEXT PRIMARY KEY,
            skill_name TEXT NOT NULL,
            skill_source TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS invocations (
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            skill_path TEXT NOT NULL,
            invoked_at TEXT NOT NULL,
            cwd TEXT,
            agent_kind TEXT NOT NULL CHECK (agent_kind IN ('main', 'subagent', 'unknown')),
            model TEXT,
            ingest_source TEXT NOT NULL CHECK (ingest_source IN ('history', 'hook')),
            UNIQUE (session_id, turn_id, skill_path),
            FOREIGN KEY (skill_path) REFERENCES skills(skill_path)
        );

        CREATE TABLE IF NOT EXISTS scan_state (
            transcript_path TEXT PRIMARY KEY,
            byte_offset INTEGER NOT NULL,
            file_size INTEGER NOT NULL,
            file_mtime INTEGER NOT NULL,
            cursor_fingerprint TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS diagnostics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transcript_path TEXT NOT NULL,
            byte_offset INTEGER NOT NULL,
            line_number INTEGER,
            event_type TEXT,
            category TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (transcript_path, byte_offset, category)
        );

        CREATE INDEX IF NOT EXISTS invocations_invoked_at_idx
            ON invocations(invoked_at);
        CREATE INDEX IF NOT EXISTS invocations_skill_path_idx
            ON invocations(skill_path);
        CREATE INDEX IF NOT EXISTS diagnostics_category_idx
            ON diagnostics(category);
        """
    )
    scan_state_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(scan_state)")
    }
    if "cursor_fingerprint" not in scan_state_columns:
        connection.execute(
            "ALTER TABLE scan_state ADD COLUMN cursor_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    connection.commit()
    return connection


def _skip_js_space(source: str, position: int) -> int:
    while position < len(source) and source[position].isspace():
        position += 1
    return position


def _exec_argument_objects(source: str) -> Iterable[str]:
    """Yield object literals from real tools.exec_command calls in JavaScript code."""
    marker = "tools.exec_command"
    position = 0
    quote: str | None = None
    line_comment = False
    block_comment = False
    escaped = False
    while position < len(source):
        char = source[position]
        following = source[position + 1] if position + 1 < len(source) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            position += 1
            continue
        if block_comment:
            if char == "*" and following == "/":
                block_comment = False
                position += 2
            else:
                position += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            position += 1
            continue
        if char in "'\"`":
            quote = char
            position += 1
            continue
        if char == "/" and following == "/":
            line_comment = True
            position += 2
            continue
        if char == "/" and following == "*":
            block_comment = True
            position += 2
            continue
        if not source.startswith(marker, position):
            position += 1
            continue
        before = source[position - 1] if position else ""
        after_position = position + len(marker)
        after = source[after_position] if after_position < len(source) else ""
        if (before and (before.isalnum() or before in "_.$")) or (
            after and (after.isalnum() or after in "_$")
        ):
            position += 1
            continue
        cursor = _skip_js_space(source, after_position)
        if cursor >= len(source) or source[cursor] != "(":
            position += len(marker)
            continue
        cursor = _skip_js_space(source, cursor + 1)
        if cursor >= len(source) or source[cursor] != "{":
            position += len(marker)
            continue

        start = cursor
        depth = 0
        inner_quote: str | None = None
        inner_escaped = False
        cursor_comment: str | None = None
        while cursor < len(source):
            current = source[cursor]
            next_char = source[cursor + 1] if cursor + 1 < len(source) else ""
            if cursor_comment == "line":
                if current in "\r\n":
                    cursor_comment = None
                cursor += 1
                continue
            if cursor_comment == "block":
                if current == "*" and next_char == "/":
                    cursor_comment = None
                    cursor += 2
                else:
                    cursor += 1
                continue
            if inner_quote:
                if inner_escaped:
                    inner_escaped = False
                elif current == "\\":
                    inner_escaped = True
                elif current == inner_quote:
                    inner_quote = None
                cursor += 1
                continue
            if current in "'\"`":
                inner_quote = current
            elif current == "/" and next_char == "/":
                cursor_comment = "line"
                cursor += 2
                continue
            elif current == "/" and next_char == "*":
                cursor_comment = "block"
                cursor += 2
                continue
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    end = cursor + 1
                    closing = _skip_js_space(source, end)
                    if closing < len(source) and source[closing] == ")":
                        yield source[start:end]
                        position = closing + 1
                    else:
                        position = end
                    break
            cursor += 1
        else:
            return


def _command_calls(tool_input: Any) -> list[tuple[str, str | None]]:
    calls: list[tuple[str, str | None]] = []
    if not isinstance(tool_input, str):
        return calls

    for argument in _exec_argument_objects(tool_input):
        try:
            value = json.loads(argument)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            command = value.get("cmd") or value.get("command")
            if isinstance(command, str):
                workdir = value.get("workdir")
                calls.append((command, str(workdir) if workdir else None))
                continue

        command_match = _CMD_JSON_STRING.search(argument)
        if command_match:
            try:
                command = json.loads(command_match.group("value"))
                workdir_match = _WORKDIR_JSON_STRING.search(argument)
                workdir = json.loads(workdir_match.group("value")) if workdir_match else None
                calls.append((command, workdir))
            except json.JSONDecodeError:
                continue
    return calls


def _expand_path(path_text: str, cwd: str | os.PathLike[str] | None) -> str | None:
    value = path_text.strip().rstrip(".,:)")
    if not value or "*" in value or "?" in value or "`" in value:
        return None

    if value.lower().startswith("file:"):
        parsed = urlparse(value)
        if parsed.scheme.lower() != "file":
            return None
        value = unquote(parsed.path)
        if parsed.netloc:
            value = f"//{parsed.netloc}{value}"
        elif re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    elif "://" in value:
        return None

    env_pattern = re.compile(r"\$(?:env:)?([A-Za-z_][A-Za-z0-9_]*)|%([^%]+)%", re.IGNORECASE)

    def replace_env(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, match.group(0))

    value = env_pattern.sub(replace_env, value)
    if "$" in value or re.search(r"%[^%]+%", value):
        return None
    value = os.path.expanduser(value)
    path = Path(value)
    if not path.is_absolute():
        path = Path(cwd) / path if cwd else Path.cwd() / path
    return os.path.normcase(str(path.resolve(strict=False)))


def _argument_tokens(arguments: str) -> list[tuple[str, int, int]]:
    tokens = []
    for match in _ARGUMENT_TOKEN.finditer(arguments):
        value = match.group()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        tokens.append((value, match.start(), match.end()))
    return tokens


def _is_skill_path(value: str) -> bool:
    return bool(re.search(r"(?:^|[/\\])SKILL\.md$", value, re.IGNORECASE))


def _sed_file_indexes(tokens: list[tuple[str, int, int]]) -> set[int]:
    files: set[int] = set()
    explicit_script = False
    script_seen = False
    skip_next = False
    for index, (value, _, _) in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        lowered = value.lower()
        if lowered in {"-e", "--expression", "-f", "--file"}:
            explicit_script = True
            skip_next = True
            continue
        if lowered.startswith(("--expression=", "--file=")) or (
            lowered.startswith(("-e", "-f")) and len(lowered) > 2
        ):
            explicit_script = True
            continue
        if value.startswith("-"):
            continue
        if not explicit_script and not script_seen:
            script_seen = True
            continue
        files.add(index)
    return files


def _file_operand_indexes(
    verb: str, tokens: list[tuple[str, int, int]]
) -> set[int]:
    if verb == "sed":
        return _sed_file_indexes(tokens)
    options_with_values = {
        "get-content": {
            "-readcount",
            "-totalcount",
            "-tail",
            "-encoding",
            "-delimiter",
            "-filter",
            "-include",
            "-exclude",
            "-stream",
        },
        "gc": {"-readcount", "-totalcount", "-tail", "-encoding", "-delimiter"},
        "head": {"-n", "--lines", "-c", "--bytes", "--label"},
        "tail": {"-n", "--lines", "-c", "--bytes", "--pid", "--sleep-interval"},
        "bat": {"-l", "--language", "--theme", "--style", "--tabs"},
    }.get(verb, set())
    files: set[int] = set()
    skip_next = False
    for index, (value, _, _) in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        lowered = value.lower()
        if lowered in options_with_values:
            skip_next = True
            continue
        if value.startswith("-"):
            continue
        files.add(index)
    return files


def _paths_from_command(command: str, cwd: str | os.PathLike[str] | None) -> set[str]:
    paths: set[str] = set()
    for read_match in _READ_COMMAND.finditer(command):
        verb = read_match.group("verb").lower()
        arguments = read_match.group("arguments")
        if _DYNAMIC_ENUMERATION.search(arguments):
            continue
        if verb == "sed" and re.search(
            r"(?:^|\s)(?:--in-place(?:=\S*)?|-[A-Za-z]*i\S*)(?=\s|$)", arguments
        ):
            continue
        tokens = _argument_tokens(arguments)
        candidate_indexes = _file_operand_indexes(verb, tokens)
        bindings: dict[str, str] = {}
        for assignment in _PS_ASSIGNMENT.finditer(command[: read_match.start()]):
            name = assignment.group("name").lower()
            literal = _QUOTED_VALUE.fullmatch(assignment.group("value").strip())
            if not literal or not _is_skill_path(literal.group("value")):
                bindings.pop(name, None)
                continue
            normalized = _expand_path(literal.group("value"), cwd)
            if normalized:
                bindings[name] = normalized
            else:
                bindings.pop(name, None)
        for index in candidate_indexes:
            value, _, _ = tokens[index]
            if value.startswith("-") or (index and tokens[index - 1][0] in {">", ">>"}):
                continue
            variable = _PS_VARIABLE.fullmatch(value)
            if variable:
                resolved = bindings.get(variable.group("name").lower())
                if resolved:
                    paths.add(resolved)
            elif _is_skill_path(value):
                normalized = _expand_path(value, cwd)
                if normalized:
                    paths.add(normalized)
    return paths


def extract_skill_paths(payload: dict[str, Any], cwd: str | os.PathLike[str] | None) -> set[str]:
    """Return concrete Skill instruction paths read by one custom tool call."""
    if payload.get("type") != "custom_tool_call":
        return set()
    name = str(payload.get("name", ""))
    tool_input = payload.get("input")
    if name.lower() == "exec":
        paths: set[str] = set()
        for command, workdir in _command_calls(tool_input):
            paths.update(_paths_from_command(command, workdir or cwd))
        return paths
    return set()


def _agent_kind(data: dict[str, Any]) -> str | None:
    for key in ("agent_kind", "agent_type"):
        value = data.get(key)
        if isinstance(value, str):
            lowered = value.lower()
            if lowered in {"main", "primary", "root"}:
                return "main"
            if lowered in {"subagent", "sub_agent", "child", "worker"}:
                return "subagent"
    if any(
        data.get(key)
        for key in ("parent_session_id", "parent_thread_id", "parent_id", "agent_path")
    ):
        return "subagent"
    source = data.get("source")
    if isinstance(source, dict):
        flattened = json.dumps(source, ensure_ascii=True).lower()
        if "subagent" in flattened or "parent_thread" in flattened or "agent_path" in flattened:
            return "subagent"
        if source.get("origin") in {"cli", "vscode", "app", "terminal", "interactive"}:
            return "main"
    elif isinstance(source, str):
        lowered = source.lower()
        if "subagent" in lowered:
            return "subagent"
        if lowered in {"cli", "vscode", "app", "terminal", "interactive"}:
            return "main"
    return None


def _update_context(event: dict[str, Any], context: dict[str, Any]) -> None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    is_session_meta = event.get("type") == "session_meta"
    if is_session_meta and context.get("identity_locked"):
        return
    if is_session_meta and not context.get("identity_locked"):
        session_value = payload.get("id") or event.get("id")
        if session_value:
            context["session_id"] = str(session_value)
            context["agent_kind"] = _agent_kind(payload) or _agent_kind(event) or "unknown"
            context["identity_locked"] = True
    elif not context.get("identity_locked"):
        for source in (event, payload):
            if source.get("session_id"):
                context["session_id"] = str(source["session_id"])
            kind = _agent_kind(source)
            if kind:
                context["agent_kind"] = kind

    for source in (event, payload):
        if source.get("cwd"):
            context["cwd"] = str(source["cwd"])
        if source.get("model"):
            context["model"] = str(source["model"])

    event_type = event.get("type")
    if event_type == "turn_context":
        context["fallback_turn"] += 1
        context["turn_id"] = payload.get("turn_id") or event.get("turn_id")
    elif (
        event_type == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "user"
    ) or (event_type == "event_msg" and payload.get("type") == "user_message"):
        context["fallback_turn"] += 1
        context["turn_id"] = None


def _turn_id(event: dict[str, Any], payload: dict[str, Any], context: dict[str, Any]) -> str:
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, dict) and metadata.get("turn_id"):
        return str(metadata["turn_id"])
    for source in (payload, event):
        if source.get("turn_id"):
            return str(source["turn_id"])
    if context.get("turn_id"):
        return str(context["turn_id"])
    return f"fallback-turn-{context['fallback_turn']}"


def _skill_metadata(skill_path: str) -> tuple[str, str]:
    path = Path(skill_path)
    name = path.parent.name or "unknown"
    try:
        with path.open("r", encoding="utf-8") as stream:
            if stream.readline().strip() == "---":
                for line_number, line in enumerate(stream, start=2):
                    if line.strip() == "---" or line_number > 80:
                        break
                    match = _FRONTMATTER_NAME.match(line)
                    if match:
                        name = match.group(1).strip().strip('"\'') or name
                        break
    except (OSError, UnicodeError):
        pass

    normalized = skill_path.replace("\\", "/").lower()
    if "/.codex/skills/.system/" in normalized:
        source = "system"
    elif "/.agents/skills/" in normalized:
        source = "agents"
    elif "/.codex/plugins/" in normalized or "/plugins/" in normalized:
        source = "plugin"
    else:
        source = "other"
    return name, source


def _diagnose(
    connection: sqlite3.Connection,
    transcript_path: str,
    byte_offset: int,
    line_number: int,
    event_type: str | None,
    category: str,
    detail: str,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO diagnostics
            (transcript_path, byte_offset, line_number, event_type, category, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transcript_path,
            byte_offset,
            line_number,
            event_type,
            category,
            detail[:300],
            _utc_now(),
        ),
    )


def _prefix_context(path: Path, stop: int, context: dict[str, Any]) -> int:
    if stop <= 0:
        return 0
    line_count = 0
    with path.open("rb") as stream:
        remaining = stop
        while remaining > 0:
            raw = stream.readline(remaining)
            if not raw:
                break
            remaining -= len(raw)
            line_count += raw.count(b"\n")
            try:
                event = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                _update_context(event, context)
    return line_count


def _cursor_fingerprint(path: Path, offset: int) -> str:
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


def scan_transcript(
    transcript_path: str | os.PathLike[str],
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    *,
    session_id: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
    model: str | None = None,
    ingest_source: str = "history",
) -> ScanResult:
    """Scan complete JSONL records added since the previous successful scan."""
    if ingest_source not in {"history", "hook"}:
        raise ValueError("ingest_source must be 'history' or 'hook'")

    path = Path(transcript_path).resolve()
    stat = path.stat()
    transcript_key = str(path)
    connection = init_db(db_path)
    try:
        state = connection.execute(
            """
            SELECT byte_offset, file_size, file_mtime, cursor_fingerprint
            FROM scan_state WHERE transcript_path = ?
            """,
            (transcript_key,),
        ).fetchone()
        offset = int(state[0]) if state else 0
        if state and (
            stat.st_size < offset
            or stat.st_size < int(state[1])
            or not state[3]
            or state[3] != _cursor_fingerprint(path, offset)
            or (
                stat.st_size == int(state[1])
                and stat.st_mtime_ns != int(state[2])
                and offset == stat.st_size
            )
        ):
            offset = 0

        context: dict[str, Any] = {
            "session_id": session_id or path.stem,
            "cwd": str(cwd) if cwd is not None else None,
            "model": model,
            "agent_kind": None,
            "identity_locked": False,
            "turn_id": None,
            "fallback_turn": 0,
        }
        prefix_lines = _prefix_context(path, offset, context)
        start_offset = offset
        with path.open("rb") as stream:
            stream.seek(offset)
            new_data = stream.read()

        complete_length = 0
        processed_lines = 0
        inserted = 0
        duplicates = 0
        parse_errors = 0
        for raw_line in new_data.splitlines(keepends=True):
            if not raw_line.endswith((b"\n", b"\r")):
                break
            line_offset = start_offset + complete_length
            complete_length += len(raw_line)
            processed_lines += 1
            line_number = prefix_lines + processed_lines
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except UnicodeDecodeError as error:
                parse_errors += 1
                _diagnose(
                    connection,
                    transcript_key,
                    line_offset,
                    line_number,
                    None,
                    "invalid_encoding",
                    f"UTF-8 decode failed at byte {error.start}",
                )
                continue
            except json.JSONDecodeError as error:
                parse_errors += 1
                _diagnose(
                    connection,
                    transcript_key,
                    line_offset,
                    line_number,
                    None,
                    "invalid_json",
                    f"{error.msg} at column {error.colno}",
                )
                continue

            if not isinstance(event, dict):
                continue
            _update_context(event, context)
            if event.get("type") != "response_item":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            paths = extract_skill_paths(payload, context.get("cwd"))
            if not paths:
                continue

            current_session = str(
                context["session_id"]
                if context.get("identity_locked")
                else event.get("session_id") or context["session_id"]
            )
            current_turn = _turn_id(event, payload, context)
            invoked_at = str(event.get("timestamp") or payload.get("timestamp") or _utc_now())
            current_kind = _agent_kind(payload) or _agent_kind(event) or context.get("agent_kind") or "unknown"
            if current_kind == "unknown":
                _diagnose(
                    connection,
                    transcript_key,
                    line_offset,
                    line_number,
                    str(event.get("type")),
                    "unknown_agent",
                    "agent metadata unavailable",
                )

            for skill_path in paths:
                skill_name, skill_source = _skill_metadata(skill_path)
                connection.execute(
                    """
                    INSERT INTO skills
                        (skill_path, skill_name, skill_source, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(skill_path) DO UPDATE SET
                        skill_name = excluded.skill_name,
                        skill_source = excluded.skill_source,
                        first_seen_at = MIN(skills.first_seen_at, excluded.first_seen_at),
                        last_seen_at = MAX(skills.last_seen_at, excluded.last_seen_at)
                    """,
                    (skill_path, skill_name, skill_source, invoked_at, invoked_at),
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO invocations
                        (session_id, turn_id, skill_path, invoked_at, cwd,
                         agent_kind, model, ingest_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current_session,
                        current_turn,
                        skill_path,
                        invoked_at,
                        context.get("cwd"),
                        current_kind,
                        context.get("model"),
                        ingest_source,
                    ),
                )
                if cursor.rowcount == 1:
                    inserted += 1
                else:
                    duplicates += 1

        new_offset = start_offset + complete_length
        connection.execute(
            """
            INSERT INTO scan_state
                (transcript_path, byte_offset, file_size, file_mtime,
                 cursor_fingerprint, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(transcript_path) DO UPDATE SET
                byte_offset = excluded.byte_offset,
                file_size = excluded.file_size,
                file_mtime = excluded.file_mtime,
                cursor_fingerprint = excluded.cursor_fingerprint,
                updated_at = excluded.updated_at
            """,
            (
                transcript_key,
                new_offset,
                stat.st_size,
                stat.st_mtime_ns,
                _cursor_fingerprint(path, new_offset),
                _utc_now(),
            ),
        )
        connection.commit()
        return ScanResult(
            files=1,
            lines=processed_lines,
            inserted=inserted,
            duplicates=duplicates,
            parse_errors=parse_errors,
            retained_bytes=len(new_data) - complete_length,
        )
    finally:
        connection.close()


def backfill(
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    codex_home: str | os.PathLike[str] | None = None,
) -> ScanResult:
    """Incrementally scan every JSONL transcript below CODEX_HOME/sessions."""
    home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    sessions = home / "sessions"
    result = ScanResult()
    if not sessions.exists():
        return result
    for transcript in sorted(sessions.rglob("*.jsonl")):
        try:
            result += scan_transcript(transcript, db_path=db_path, ingest_source="history")
        except (OSError, UnicodeError, sqlite3.Error, ValueError) as error:
            connection = init_db(db_path)
            try:
                _diagnose(
                    connection,
                    str(transcript.resolve()),
                    0,
                    0,
                    None,
                    "scan_failure",
                    type(error).__name__,
                )
                connection.commit()
            finally:
                connection.close()
            result += ScanResult(files=1)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backfill", help="scan all transcripts below CODEX_HOME/sessions")
    scan_parser = subparsers.add_parser("scan", help="incrementally scan one transcript")
    scan_parser.add_argument("transcript_path")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "backfill":
        result = backfill()
    else:
        result = scan_transcript(args.transcript_path)
    print(json.dumps(result.__dict__, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
