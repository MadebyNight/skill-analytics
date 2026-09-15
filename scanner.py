"""Extract Skill reads from verified response_item/custom_tool_call/exec events."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from adapters.base import EVIDENCE_PRIORITY, InstalledSkill, Invocation, canonical_path


PROJECT_ROOT = Path(__file__).resolve().parent
LEGACY_DATA_DIR = PROJECT_ROOT / "data"
LEGACY_DB_PATH = LEGACY_DATA_DIR / "analytics.db"


def _user_data_dir(platform: str | None = None) -> Path:
    """Return the per-user analytics data directory for the current OS."""
    override = os.environ.get("SKILL_ANALYTICS_HOME")
    if override:
        return Path(override).expanduser()
    if (platform or os.name) == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base).expanduser() if base else Path.home() / "AppData" / "Local"
        return root / "skill-analytics"
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "skill-analytics"


DATA_DIR = _user_data_dir()
DEFAULT_DB_PATH = DATA_DIR / "analytics.db"


def migrate_legacy_data(legacy_db: str | os.PathLike[str] = LEGACY_DB_PATH) -> bool:
    """Copy a pre-existing repository-local database to the user data directory once.

    Returns True when a migration happened. Existing user data always wins.
    """
    source = Path(legacy_db)
    if source.resolve() == DEFAULT_DB_PATH.resolve():
        return False
    if DEFAULT_DB_PATH.exists() or not source.exists():
        return False
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, DEFAULT_DB_PATH)
    return True


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
    upgraded: int = 0
    failures: int = 0

    def __add__(self, other: "ScanResult") -> "ScanResult":
        return ScanResult(
            files=self.files + other.files,
            lines=self.lines + other.lines,
            inserted=self.inserted + other.inserted,
            duplicates=self.duplicates + other.duplicates,
            parse_errors=self.parse_errors + other.parse_errors,
            retained_bytes=self.retained_bytes + other.retained_bytes,
            upgraded=self.upgraded + other.upgraded,
            failures=self.failures + other.failures,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _create_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS installed_skills (
            platform TEXT NOT NULL,
            skill_key TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            skill_path TEXT,
            skill_source TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (platform, skill_key)
        )""",
        """CREATE TABLE IF NOT EXISTS invocations (
            platform TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            skill_path TEXT,
            skill_key TEXT NOT NULL,
            evidence_type TEXT NOT NULL CHECK (
                evidence_type IN ('structured_skill', 'slash_skill', 'skill_file_read')
            ),
            invoked_at TEXT NOT NULL,
            cwd TEXT,
            agent_kind TEXT NOT NULL CHECK (agent_kind IN ('main', 'subagent', 'unknown')),
            model TEXT,
            ingest_source TEXT NOT NULL CHECK (ingest_source IN ('history', 'realtime')),
            PRIMARY KEY (platform, session_id, turn_id, skill_key)
        )""",
        """CREATE TABLE IF NOT EXISTS scan_state (
            platform TEXT NOT NULL,
            source_path TEXT NOT NULL,
            byte_offset INTEGER NOT NULL,
            file_size INTEGER NOT NULL,
            file_mtime INTEGER NOT NULL,
            cursor_fingerprint TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (platform, source_path)
        )""",
        """CREATE TABLE IF NOT EXISTS diagnostics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            source_path TEXT NOT NULL,
            byte_offset INTEGER NOT NULL,
            line_number INTEGER,
            event_type TEXT,
            category TEXT NOT NULL,
            detail TEXT NOT NULL,
            adapter_version TEXT,
            format_version TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (platform, source_path, byte_offset, category)
        )""",
        """CREATE TABLE IF NOT EXISTS platform_status (
            platform TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN (
                'ready', 'not_installed', 'unsupported_version', 'partial', 'integration_error'
            )),
            resolved_root TEXT,
            last_history_scan_at TEXT,
            last_realtime_at TEXT,
            adapter_version TEXT,
            format_version TEXT,
            updated_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS invocations_invoked_at_idx ON invocations(invoked_at)",
        "CREATE INDEX IF NOT EXISTS invocations_skill_path_idx ON invocations(skill_path)",
        "CREATE INDEX IF NOT EXISTS diagnostics_category_idx ON diagnostics(category)",
    )
    for statement in statements:
        connection.execute(statement)


def _validate_migration(connection: sqlite3.Connection, expected_invocations: int) -> None:
    migrated = connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
    if migrated != expected_invocations:
        raise sqlite3.IntegrityError(
            f"invocation migration count changed: {expected_invocations} -> {migrated}"
        )


def _migrate_legacy_schema(connection: sqlite3.Connection) -> None:
    old_invocation_count = connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
    legacy_tables = tuple(
        table for table in ("skills", "invocations", "scan_state", "diagnostics")
        if _table_exists(connection, table)
    )
    for table in legacy_tables:
        connection.execute(f'ALTER TABLE "{table}" RENAME TO "legacy_{table}"')
    _create_schema(connection)

    if "skills" in legacy_tables:
        connection.execute(
            """INSERT INTO installed_skills
                (platform, skill_key, skill_name, skill_path, skill_source,
                 first_seen_at, last_seen_at)
            SELECT 'codex', skill_path, skill_name, skill_path, skill_source,
                   first_seen_at, last_seen_at
            FROM legacy_skills"""
        )
    connection.execute(
        """INSERT INTO invocations
            (platform, session_id, turn_id, skill_name, skill_path, skill_key,
             evidence_type, invoked_at, cwd, agent_kind, model, ingest_source)
        SELECT 'codex', i.session_id, i.turn_id, s.skill_name, i.skill_path, i.skill_path,
               'skill_file_read', i.invoked_at, i.cwd, i.agent_kind, i.model,
               CASE i.ingest_source WHEN 'hook' THEN 'realtime' ELSE 'history' END
        FROM legacy_invocations AS i
        JOIN legacy_skills AS s ON s.skill_path = i.skill_path"""
    )
    if "scan_state" in legacy_tables:
        scan_columns = _columns(connection, "legacy_scan_state")
        fingerprint = "cursor_fingerprint" if "cursor_fingerprint" in scan_columns else "''"
        connection.execute(
            f"""INSERT INTO scan_state
                (platform, source_path, byte_offset, file_size, file_mtime,
                 cursor_fingerprint, updated_at)
            SELECT 'codex', transcript_path, byte_offset, file_size, file_mtime,
                   {fingerprint}, updated_at
            FROM legacy_scan_state"""
        )
    if "diagnostics" in legacy_tables:
        connection.execute(
            """INSERT INTO diagnostics
                (platform, source_path, byte_offset, line_number, event_type, category,
                 detail, adapter_version, format_version, created_at)
            SELECT 'codex', transcript_path, byte_offset, line_number, event_type, category,
                   detail, 'legacy', 'legacy', created_at
            FROM legacy_diagnostics"""
        )

    _validate_migration(connection, old_invocation_count)
    for table in ("invocations", "skills", "scan_state", "diagnostics"):
        if table in legacy_tables:
            connection.execute(f'DROP TABLE "legacy_{table}"')
    _create_schema(connection)


def init_db(db_path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create or atomically migrate the analytics schema and return a connection."""
    path = Path(db_path)
    if path.resolve() == DEFAULT_DB_PATH.resolve():
        migrate_legacy_data()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=2.0)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if _table_exists(connection, "invocations") and "platform" not in _columns(
            connection, "invocations"
        ):
            _migrate_legacy_schema(connection)
        else:
            _create_schema(connection)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
    except BaseException:
        connection.rollback()
        connection.close()
        raise
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


def upsert_installed_skill(
    connection: sqlite3.Connection, skill: InstalledSkill
) -> None:
    path = canonical_path(skill.skill_path) if skill.skill_path else None
    key = path or skill.skill_key or f"name:{skill.skill_name.casefold()}"
    connection.execute(
        """INSERT INTO installed_skills
            (platform, skill_key, skill_name, skill_path, skill_source,
             first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(platform, skill_key) DO UPDATE SET
            skill_name = excluded.skill_name,
            skill_path = excluded.skill_path,
            skill_source = excluded.skill_source,
            first_seen_at = MIN(installed_skills.first_seen_at, excluded.first_seen_at),
            last_seen_at = MAX(installed_skills.last_seen_at, excluded.last_seen_at)""",
        (
            skill.platform,
            key,
            skill.skill_name,
            path,
            skill.skill_source,
            skill.first_seen_at,
            skill.last_seen_at,
        ),
    )


def store_installed_skills(
    skills: Iterable[InstalledSkill],
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
) -> None:
    """Store platform discovery results without removing historical mappings."""
    connection = init_db(db_path)
    try:
        for skill in skills:
            upsert_installed_skill(connection, skill)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def replace_installed_skills(
    platform: str,
    skills: Iterable[InstalledSkill],
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    *,
    complete: bool,
) -> None:
    """Atomically refresh inventory, pruning stale rows only after a complete discovery."""
    current = list(skills)
    if any(skill.platform != platform for skill in current):
        raise ValueError("all installed Skills must belong to the replaced platform")
    current_keys = [
        canonical_path(skill.skill_path)
        if skill.skill_path
        else skill.skill_key or f"name:{skill.skill_name.casefold()}"
        for skill in current
    ]
    connection = init_db(db_path)
    try:
        for skill in current:
            upsert_installed_skill(connection, skill)
        if complete:
            if current_keys:
                placeholders = ", ".join("?" for _ in current_keys)
                connection.execute(
                    f"DELETE FROM installed_skills WHERE platform = ? "
                    f"AND skill_key NOT IN ({placeholders})",
                    (platform, *current_keys),
                )
            else:
                connection.execute(
                    "DELETE FROM installed_skills WHERE platform = ?", (platform,)
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _write_platform_status(
    connection: sqlite3.Connection,
    platform: str,
    status: str,
    resolved_root: str | os.PathLike[str] | None,
    last_history_scan_at: str | None,
    last_realtime_at: str | None,
    adapter_version: str | None,
    format_version: str | None,
) -> None:
    connection.execute(
        """INSERT INTO platform_status
            (platform, status, resolved_root, last_history_scan_at, last_realtime_at,
             adapter_version, format_version, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(platform) DO UPDATE SET
            status = excluded.status,
            resolved_root = COALESCE(excluded.resolved_root, platform_status.resolved_root),
            last_history_scan_at = COALESCE(
                excluded.last_history_scan_at, platform_status.last_history_scan_at
            ),
            last_realtime_at = COALESCE(
                excluded.last_realtime_at, platform_status.last_realtime_at
            ),
            adapter_version = COALESCE(
                excluded.adapter_version, platform_status.adapter_version
            ),
            format_version = COALESCE(
                excluded.format_version, platform_status.format_version
            ),
            updated_at = excluded.updated_at""",
        (
            platform,
            status,
            str(resolved_root) if resolved_root is not None else None,
            last_history_scan_at,
            last_realtime_at,
            adapter_version,
            format_version,
            _utc_now(),
        ),
    )


def update_platform_status(
    platform: str,
    status: str,
    *,
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    resolved_root: str | os.PathLike[str] | None = None,
    last_history_scan_at: str | None = None,
    last_realtime_at: str | None = None,
    adapter_version: str | None = None,
    format_version: str | None = None,
) -> None:
    """Persist one platform's latest independent discovery or scan status."""
    connection = init_db(db_path)
    try:
        _write_platform_status(
            connection,
            platform,
            status,
            resolved_root,
            last_history_scan_at,
            last_realtime_at,
            adapter_version,
            format_version,
        )
        connection.commit()
    finally:
        connection.close()


def _normalized_invocation(
    connection: sqlite3.Connection, invocation: Invocation
) -> Invocation:
    if invocation.evidence_type not in EVIDENCE_PRIORITY:
        raise ValueError(f"unknown evidence type: {invocation.evidence_type}")
    if invocation.agent_kind not in {"main", "subagent", "unknown"}:
        raise ValueError(f"unknown agent kind: {invocation.agent_kind}")
    if not invocation.skill_name.strip():
        raise ValueError("skill_name must not be empty")

    path = canonical_path(invocation.skill_path) if invocation.skill_path else None
    key = path
    if key is None:
        matches = [
            row[:2]
            for row in connection.execute(
                """SELECT skill_key, skill_path, skill_name FROM installed_skills
                   WHERE platform = ?""",
                (invocation.platform,),
            )
            if str(row[2]).casefold() == invocation.skill_name.casefold()
        ]
        if len(matches) == 1:
            key, path = str(matches[0][0]), matches[0][1]
        else:
            key = f"name:{invocation.skill_name.casefold()}"
            if len(matches) > 1:
                _diagnose(
                    connection,
                    f"invocation:{invocation.session_id}:{invocation.turn_id}",
                    0,
                    0,
                    invocation.evidence_type,
                    "ambiguous_skill_identity",
                    "multiple installed Skills have the same case-folded name",
                    platform=invocation.platform,
                )

    source = "realtime" if invocation.ingest_source == "hook" else invocation.ingest_source
    if source not in {"history", "realtime"}:
        raise ValueError(f"unknown ingest source: {invocation.ingest_source}")
    return Invocation(
        platform=invocation.platform,
        session_id=invocation.session_id,
        turn_id=invocation.turn_id,
        skill_name=invocation.skill_name,
        skill_path=str(path) if path is not None else None,
        skill_key=str(key),
        evidence_type=invocation.evidence_type,
        invoked_at=invocation.invoked_at,
        cwd=invocation.cwd,
        agent_kind=invocation.agent_kind,
        model=invocation.model,
        ingest_source=source,
    )


def ingest_invocation(
    connection: sqlite3.Connection, invocation: Invocation
) -> str:
    """Insert, upgrade, or deduplicate one normalized invocation."""
    invocation = _normalized_invocation(connection, invocation)
    same_turn = connection.execute(
        """SELECT skill_key, skill_name, skill_path, evidence_type, invoked_at, cwd,
                  agent_kind, model, ingest_source
           FROM invocations
           WHERE platform = ? AND session_id = ? AND turn_id = ?""",
        (invocation.platform, invocation.session_id, invocation.turn_id),
    ).fetchall()
    matching_paths = {
        row[0]: row[2]
        for row in same_turn
        if row[2] and str(row[1]).casefold() == invocation.skill_name.casefold()
    }
    if invocation.skill_path is None and len(matching_paths) == 1:
        key, path = next(iter(matching_paths.items()))
        invocation = replace(invocation, skill_key=str(key), skill_path=str(path))

    if invocation.skill_path:
        fallback_key = f"name:{invocation.skill_name.casefold()}"
        target = next((row for row in same_turn if row[0] == invocation.skill_key), None)
        fallback = next((row for row in same_turn if row[0] == fallback_key), None)
        if target is not None and fallback is not None and target[0] != fallback[0]:
            if EVIDENCE_PRIORITY[fallback[3]] > EVIDENCE_PRIORITY[target[3]]:
                connection.execute(
                    """UPDATE invocations
                       SET skill_name = ?, evidence_type = ?, invoked_at = ?,
                           cwd = COALESCE(?, cwd), agent_kind = ?,
                           model = COALESCE(?, model), ingest_source = ?
                       WHERE platform = ? AND session_id = ? AND turn_id = ? AND skill_key = ?""",
                    (
                        fallback[1], fallback[3], fallback[4], fallback[5], fallback[6],
                        fallback[7], fallback[8], invocation.platform, invocation.session_id,
                        invocation.turn_id, invocation.skill_key,
                    ),
                )
            connection.execute(
                """DELETE FROM invocations
                   WHERE platform = ? AND session_id = ? AND turn_id = ? AND skill_key = ?""",
                (
                    invocation.platform,
                    invocation.session_id,
                    invocation.turn_id,
                    fallback_key,
                ),
            )
        connection.execute(
            """UPDATE OR IGNORE invocations
               SET skill_key = ?, skill_path = ?, skill_name = ?
               WHERE platform = ? AND session_id = ? AND turn_id = ? AND skill_key = ?""",
            (
                invocation.skill_key,
                invocation.skill_path,
                invocation.skill_name,
                invocation.platform,
                invocation.session_id,
                invocation.turn_id,
                fallback_key,
            ),
        )
    key = (
        invocation.platform,
        invocation.session_id,
        invocation.turn_id,
        invocation.skill_key,
    )
    cursor = connection.execute(
        """INSERT OR IGNORE INTO invocations
            (platform, session_id, turn_id, skill_name, skill_path, skill_key,
             evidence_type, invoked_at, cwd, agent_kind, model, ingest_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            invocation.platform,
            invocation.session_id,
            invocation.turn_id,
            invocation.skill_name,
            invocation.skill_path,
            invocation.skill_key,
            invocation.evidence_type,
            invocation.invoked_at,
            invocation.cwd,
            invocation.agent_kind,
            invocation.model,
            invocation.ingest_source,
        ),
    )
    if cursor.rowcount == 1:
        return "inserted"
    cursor = connection.execute(
        """UPDATE invocations
           SET skill_name = ?, skill_path = COALESCE(?, skill_path), evidence_type = ?,
               cwd = COALESCE(?, cwd), agent_kind = ?, model = COALESCE(?, model),
               ingest_source = ?
           WHERE platform = ? AND session_id = ? AND turn_id = ? AND skill_key = ?
             AND CASE evidence_type
                   WHEN 'structured_skill' THEN 3
                   WHEN 'slash_skill' THEN 2
                   ELSE 1
                 END < ?""",
        (
            invocation.skill_name,
            invocation.skill_path,
            invocation.evidence_type,
            invocation.cwd,
            invocation.agent_kind,
            invocation.model,
            invocation.ingest_source,
            *key,
            EVIDENCE_PRIORITY[invocation.evidence_type],
        ),
    )
    return "upgraded" if cursor.rowcount == 1 else "duplicate"


def store_invocations(
    invocations: Iterable[Invocation],
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
) -> ScanResult:
    """Atomically store a batch emitted by any platform adapter."""
    connection = init_db(db_path)
    inserted = duplicates = upgraded = 0
    try:
        for invocation in invocations:
            outcome = ingest_invocation(connection, invocation)
            inserted += outcome == "inserted"
            duplicates += outcome == "duplicate"
            upgraded += outcome == "upgraded"
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return ScanResult(inserted=inserted, duplicates=duplicates, upgraded=upgraded)


def _diagnose(
    connection: sqlite3.Connection,
    transcript_path: str,
    byte_offset: int,
    line_number: int,
    event_type: str | None,
    category: str,
    detail: str,
    *,
    platform: str = "codex",
    adapter_version: str | None = "1",
    format_version: str | None = "response_item/custom_tool_call/exec",
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO diagnostics
            (platform, source_path, byte_offset, line_number, event_type, category, detail,
             adapter_version, format_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            platform,
            transcript_path,
            byte_offset,
            line_number,
            event_type,
            category,
            detail[:300],
            adapter_version,
            format_version,
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
            FROM scan_state WHERE platform = 'codex' AND source_path = ?
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
                upsert_installed_skill(
                    connection,
                    InstalledSkill(
                        platform="codex",
                        skill_key=skill_path,
                        skill_name=skill_name,
                        skill_path=skill_path,
                        skill_source=skill_source,
                        first_seen_at=invoked_at,
                        last_seen_at=invoked_at,
                    ),
                )
                outcome = ingest_invocation(
                    connection,
                    Invocation(
                        platform="codex",
                        session_id=current_session,
                        turn_id=current_turn,
                        skill_name=skill_name,
                        skill_path=skill_path,
                        skill_key=skill_path,
                        evidence_type="skill_file_read",
                        invoked_at=invoked_at,
                        cwd=context.get("cwd"),
                        agent_kind=current_kind,
                        model=context.get("model"),
                        ingest_source=ingest_source,
                    ),
                )
                if outcome == "inserted":
                    inserted += 1
                else:
                    duplicates += 1

        new_offset = start_offset + complete_length
        connection.execute(
            """
            INSERT INTO scan_state
                (platform, source_path, byte_offset, file_size, file_mtime,
                 cursor_fingerprint, updated_at)
            VALUES ('codex', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, source_path) DO UPDATE SET
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
        now = _utc_now()
        _write_platform_status(
            connection,
            "codex",
            "partial" if parse_errors else "ready",
            None,
            now if ingest_source == "history" else None,
            now if ingest_source == "hook" else None,
            "1",
            "response_item/custom_tool_call/exec",
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
            result += ScanResult(files=1, failures=1)
    now = _utc_now()
    connection = init_db(db_path)
    try:
        _write_platform_status(
            connection,
            "codex",
            "partial" if result.failures or result.parse_errors else "ready",
            home.resolve(strict=False),
            now,
            None,
            "1",
            "response_item/custom_tool_call/exec",
        )
        connection.commit()
    finally:
        connection.close()
    return result


def prune_invocations(
    older_than_days: int,
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    *,
    apply: bool = False,
) -> dict[str, int]:
    """Delete Skill invocations older than N days. Reports before deleting."""
    if older_than_days < 0:
        raise ValueError("older_than_days must not be negative")
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=older_than_days)
    ).isoformat().replace("+00:00", "Z")
    connection = init_db(db_path)
    try:
        total = int(connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0])
        matched = int(
            connection.execute(
                "SELECT COUNT(*) FROM invocations WHERE invoked_at < ?", (cutoff,)
            ).fetchone()[0]
        )
        deleted = 0
        if apply and matched:
            connection.execute("DELETE FROM invocations WHERE invoked_at < ?", (cutoff,))
            connection.commit()
            deleted = matched
        return {
            "matched": matched,
            "deleted": deleted,
            "remaining": total - deleted,
            "cutoff": cutoff,
        }
    finally:
        connection.close()


def prune_diagnostics(
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    *,
    apply: bool = False,
) -> dict[str, int]:
    """Delete diagnostic rows. These are for troubleshooting only, not statistics."""
    connection = init_db(db_path)
    try:
        total = int(connection.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0])
        deleted = 0
        if apply and total:
            connection.execute("DELETE FROM diagnostics")
            connection.commit()
            deleted = total
        return {"matched": total, "deleted": deleted, "remaining": total - deleted}
    finally:
        connection.close()


def prune_dead_scan_state(
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    *,
    apply: bool = False,
) -> dict[str, int]:
    """Drop scan cursors whose source transcript no longer exists."""
    connection = init_db(db_path)
    try:
        rows = connection.execute(
            "SELECT platform, source_path FROM scan_state"
        ).fetchall()
        dead = [
            (platform, source_path)
            for platform, source_path in rows
            if not Path(source_path).exists()
        ]
        deleted = 0
        if apply and dead:
            connection.executemany(
                "DELETE FROM scan_state WHERE platform = ? AND source_path = ?", dead
            )
            connection.commit()
            deleted = len(dead)
        return {
            "matched": len(dead),
            "deleted": deleted,
            "remaining": len(rows) - deleted,
        }
    finally:
        connection.close()


def compact_database(db_path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> dict[str, int]:
    """Reclaim disk space left behind by deleted rows."""
    path = Path(db_path)
    before = path.stat().st_size if path.exists() else 0
    connection = sqlite3.connect(path, timeout=30.0)
    try:
        connection.execute("VACUUM")
    finally:
        connection.close()
    after = path.stat().st_size if path.exists() else 0
    return {"before_bytes": before, "after_bytes": after, "reclaimed_bytes": before - after}


def database_size(db_path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> int:
    path = Path(db_path)
    return path.stat().st_size if path.exists() else 0


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
