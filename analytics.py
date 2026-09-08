"""Unified command line interface for local multi-platform Skill analytics."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import report
import scanner
from adapters import ClaudeAdapter, CodexAdapter, Invocation, OpenCodeAdapter, PiAdapter
from adapters.base import InstalledSkill, PLATFORMS
from adapters.claude import parse_post_tool_use, parse_user_prompt_expansion
from adapters.pi import parse_realtime_event as parse_pi_realtime_event


ADAPTER_CLASSES = {
    "codex": CodexAdapter,
    "claude": ClaudeAdapter,
    "opencode": OpenCodeAdapter,
    "pi": PiAdapter,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--claude-home", type=Path)
    parser.add_argument("--opencode-config-dir", type=Path)
    parser.add_argument("--opencode-command", default="opencode")
    parser.add_argument("--pi-home", type=Path)
    parser.add_argument("--pi-session-dir", type=Path)
    parser.add_argument("--db", type=Path, default=scanner.DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=report.DEFAULT_OUTPUT_PATH)


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common_options(common)
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("scan-all", parents=[common], help="scan every platform")
    scan = subparsers.add_parser("scan", parents=[common], help="scan selected platforms")
    scan.add_argument("--platform", action="append", choices=PLATFORMS, required=True)
    subparsers.add_parser("report", parents=[common], help="generate the offline dashboard")
    subparsers.add_parser("status", parents=[common], help="show platform discovery and scan status")
    for command in ("install", "uninstall"):
        installer = subparsers.add_parser(command, parents=[common])
        installer.add_argument("--platform", action="append", choices=PLATFORMS)
    record = subparsers.add_parser(
        "record", parents=[common], help="record one realtime event from stdin"
    )
    record.add_argument("--platform", choices=("claude", "opencode", "pi"), required=True)
    return parser


def create_adapter(platform: str, args: argparse.Namespace):
    constructor = ADAPTER_CLASSES[platform]
    if platform == "codex":
        return constructor(codex_home=args.codex_home, db_path=args.db)
    if platform == "claude":
        return constructor(claude_home=args.claude_home, db_path=args.db)
    if platform == "opencode":
        return constructor(
            config_dir=args.opencode_config_dir,
            command=args.opencode_command,
            db_path=args.db,
        )
    return constructor(
        pi_home=args.pi_home,
        session_dir=args.pi_session_dir,
        db_path=args.db,
    )


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _stored_platform_status(db_path: Path, platform: str) -> str | None:
    connection = scanner.init_db(db_path)
    try:
        row = connection.execute(
            "SELECT status FROM platform_status WHERE platform = ?", (platform,)
        ).fetchone()
        return str(row[0]) if row else None
    finally:
        connection.close()


def _scan(args: argparse.Namespace, platforms: Iterable[str]) -> int:
    output: dict[str, Any] = {"command": args.command, "platforms": {}}
    failed = False
    for platform in _unique(platforms):
        try:
            adapter = create_adapter(platform, args)
            root = str(adapter.resolved_root)
            if getattr(adapter, "installed", None) is False:
                scanner.update_platform_status(
                    platform,
                    "not_installed",
                    db_path=args.db,
                    resolved_root=root,
                    adapter_version=getattr(adapter, "adapter_version", None),
                    format_version=getattr(adapter, "format_version", None),
                )
                output["platforms"][platform] = {
                    "status": "not_installed",
                    "resolved_root": root,
                    "result": asdict(scanner.ScanResult()),
                }
                continue
            result = adapter.scan()
            status = _stored_platform_status(args.db, platform)
            if status is None:
                status = "partial" if result.failures or result.parse_errors else "ready"
            failed = failed or status in {
                "partial", "unsupported_version", "integration_error"
            }
            output["platforms"][platform] = {
                "status": status,
                "resolved_root": root,
                "result": asdict(result),
            }
        except Exception as error:
            failed = True
            output["platforms"][platform] = {"status": "partial", "error": type(error).__name__}
            try:
                scanner.update_platform_status(platform, "partial", db_path=args.db)
            except Exception:
                pass
    print(json.dumps(output, ensure_ascii=False))
    return 1 if failed else 0


def _parse_opencode_realtime_event(payload: object) -> Invocation | None:
    if not isinstance(payload, dict) or payload.get("tool") != "skill":
        return None
    arguments = payload.get("args")
    name = arguments.get("name") if isinstance(arguments, dict) else None
    session_id = payload.get("sessionID")
    call_id = payload.get("callID")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (name, session_id, call_id)
    ):
        return None
    skill_name = name.strip()
    return Invocation(
        platform="opencode",
        session_id=session_id.strip(),
        turn_id=call_id.strip(),
        skill_name=skill_name,
        skill_path=None,
        skill_key=f"name:{skill_name.casefold()}",
        evidence_type="structured_skill",
        invoked_at=_utc_now(),
        cwd=None,
        agent_kind="unknown",
        model=None,
        ingest_source="realtime",
    )


def _parse_realtime_event(platform: str, payload: object) -> Invocation | None:
    if platform == "claude":
        if not isinstance(payload, dict):
            return None
        return parse_post_tool_use(payload) or parse_user_prompt_expansion(payload)
    if platform == "opencode":
        return _parse_opencode_realtime_event(payload)
    return parse_pi_realtime_event(payload)


def _claude_installed_skills(db_path: Path) -> list[InstalledSkill]:
    connection = scanner.init_db(db_path)
    try:
        rows = connection.execute(
            "SELECT platform, skill_key, skill_name, skill_path, skill_source, "
            "first_seen_at, last_seen_at FROM installed_skills WHERE platform = 'claude'"
        ).fetchall()
    finally:
        connection.close()
    return [InstalledSkill(*row) for row in rows]


def _diagnose_pending_claude_slash(db_path: Path) -> None:
    connection = scanner.init_db(db_path)
    try:
        scanner._diagnose(
            connection,
            "realtime:UserPromptExpansion",
            0,
            0,
            "UserPromptExpansion",
            "slash_transcript_pending",
            "slash event was ignored",
            platform="claude",
            adapter_version="1",
            format_version="hook/UserPromptExpansion",
        )
        connection.commit()
    finally:
        connection.close()


def _claude_slash_count(
    db_path: Path, session_id: object, skill_key: str
) -> int:
    if not isinstance(session_id, str) or not session_id:
        return 0
    connection = scanner.init_db(db_path)
    try:
        return int(connection.execute(
            "SELECT COUNT(*) FROM invocations WHERE platform = 'claude' "
            "AND session_id = ? AND skill_key = ? AND evidence_type = 'slash_skill'",
            (session_id, skill_key),
        ).fetchone()[0])
    finally:
        connection.close()


def _record_claude_slash(
    args: argparse.Namespace, payload: dict[str, Any]
) -> scanner.ScanResult | None:
    name = payload.get("command_name")
    if not isinstance(name, str) or not name.strip():
        return None
    installed = _claude_installed_skills(args.db)
    matches = [
        skill for skill in installed
        if skill.skill_name.casefold() == name.strip().lstrip("/").casefold()
    ]
    if len(matches) != 1:
        return None
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript.strip():
        before = _claude_slash_count(args.db, payload.get("session_id"), matches[0].skill_key)
        adapter = ClaudeAdapter(claude_home=args.claude_home, db_path=args.db)
        by_name: dict[str, list[InstalledSkill]] = {}
        for skill in installed:
            by_name.setdefault(skill.skill_name.casefold(), []).append(skill)
        result = adapter._scan_transcript(Path(transcript), by_name)
        after = _claude_slash_count(args.db, payload.get("session_id"), matches[0].skill_key)
        if after > before:
            return result
    _diagnose_pending_claude_slash(args.db)
    return None


def _record(args: argparse.Namespace) -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError):
        print(json.dumps({"command": "record", "platform": args.platform, "error": "invalid_json"}))
        return 1
    try:
        if (
            args.platform == "claude"
            and isinstance(payload, dict)
            and payload.get("hook_event_name") == "UserPromptExpansion"
        ):
            result = _record_claude_slash(args, payload)
        else:
            invocation = _parse_realtime_event(args.platform, payload)
            result = (
                scanner.store_invocations([invocation], args.db)
                if invocation is not None
                else None
            )
        if result is None:
            print(json.dumps({
                "command": "record", "platform": args.platform, "status": "ignored"
            }))
            return 0
        scanner.update_platform_status(
            args.platform,
            "partial" if result.failures or result.parse_errors else "ready",
            db_path=args.db,
            last_realtime_at=_utc_now(),
        )
    except Exception as error:
        print(json.dumps({
            "command": "record", "platform": args.platform,
            "status": "integration_error", "error": type(error).__name__,
        }))
        return 1
    print(json.dumps({
        "command": "record", "platform": args.platform,
        "status": "recorded", "result": asdict(result),
    }))
    return 0


def _status(args: argparse.Namespace) -> int:
    stored: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, list[dict[str, Any]]] = {platform: [] for platform in PLATFORMS}
    connection = scanner.init_db(args.db)
    try:
        connection.row_factory = sqlite3.Row
        stored = {
            row["platform"]: dict(row)
            for row in connection.execute("SELECT * FROM platform_status")
        }
        for row in connection.execute(
            "SELECT platform, category, COUNT(*) AS count FROM diagnostics "
            "GROUP BY platform, category ORDER BY platform, category"
        ):
            diagnostics.setdefault(row["platform"], []).append(
                {"category": row["category"], "count": row["count"]}
            )
    finally:
        connection.close()

    try:
        import install_hook

        integration_status = getattr(install_hook, "integration_status", None)
    except ImportError:
        integration_status = None
    overrides = {
        "codex_home": args.codex_home,
        "claude_home": args.claude_home,
        "opencode_config_dir": args.opencode_config_dir,
        "pi_home": args.pi_home,
        "db_path": args.db,
        "output_path": args.output,
    }
    platforms: dict[str, dict[str, Any]] = {}
    for platform in PLATFORMS:
        try:
            adapter = create_adapter(platform, args)
            current = stored.get(platform, {})
            installed = getattr(adapter, "installed", None)
            status = current.get("status", "unknown")
            if installed is False:
                status = "not_installed"
            platforms[platform] = {
                "status": status,
                "resolved_root": str(adapter.resolved_root),
                "last_history_scan_at": current.get("last_history_scan_at"),
                "last_realtime_at": current.get("last_realtime_at"),
                "adapter_version": current.get("adapter_version", getattr(adapter, "adapter_version", None)),
                "format_version": current.get("format_version", getattr(adapter, "format_version", None)),
                "updated_at": current.get("updated_at"),
                "diagnostics": diagnostics.get(platform, []),
            }
            try:
                platforms[platform]["integration_status"] = (
                    integration_status(platform, **overrides)
                    if integration_status is not None
                    else "unknown"
                )
            except Exception:
                platforms[platform]["integration_status"] = "unknown"
        except Exception as error:
            platforms[platform] = {"status": "partial", "error": type(error).__name__}
    print(json.dumps({"command": "status", "platforms": platforms}, ensure_ascii=False))
    return 0


def _install(args: argparse.Namespace) -> int:
    if not args.platform:
        print(json.dumps({"command": args.command, "error": "platform_required"}))
        return 2
    import install_hook

    operation = getattr(install_hook, f"{args.command}_platform", None)
    output: dict[str, Any] = {"command": args.command, "platforms": {}}
    if operation is None:
        output["platforms"] = {
            platform: {"error": "installer_api_unavailable"}
            for platform in _unique(args.platform)
        }
        print(json.dumps(output, ensure_ascii=False))
        return 1
    failed = False
    for platform in _unique(args.platform):
        try:
            result = operation(
                platform,
                codex_home=args.codex_home,
                claude_home=args.claude_home,
                opencode_config_dir=args.opencode_config_dir,
                pi_home=args.pi_home,
                db_path=args.db,
                output_path=args.output,
            )
            if isinstance(result, dict):
                result.setdefault("platform", platform)
                result.setdefault("action", args.command)
                result.setdefault(
                    "changed", result.get("status") in {"installed", "removed"}
                )
            output["platforms"][platform] = result
        except Exception as error:
            failed = True
            output["platforms"][platform] = {"error": type(error).__name__}
    print(json.dumps(output, ensure_ascii=False, default=str))
    return 1 if failed else 0


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "scan-all":
        return _scan(args, PLATFORMS)
    if args.command == "scan":
        return _scan(args, args.platform)
    if args.command == "report":
        output = report.generate_report(
            args.db, args.output, codex_home=args.codex_home
        )
        print(json.dumps({"command": "report", "output": str(output)}, ensure_ascii=False))
        return 0
    if args.command == "status":
        return _status(args)
    if args.command == "record":
        return _record(args)
    return _install(args)


if __name__ == "__main__":
    raise SystemExit(main())
