import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import analytics
import scanner
from adapters.base import InstalledSkill


class _Adapter:
    def __init__(self, platform, result=None, error=None, installed=True):
        self.platform = platform
        self.resolved_root = Path(f"/{platform}")
        self.installed = installed
        self._result = result or scanner.ScanResult()
        self._error = error

    def scan(self):
        if self._error:
            raise self._error
        return self._result


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "analytics.db"

    def _run(self, *arguments, stdin=""):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = analytics.main(arguments)
        payload = json.loads(stdout.getvalue()) if stdout.getvalue() else None
        return code, payload, stderr.getvalue()

    def test_scan_repeated_platforms_are_deduplicated_in_requested_order(self):
        adapters = {
            "claude": _Adapter("claude", scanner.ScanResult(inserted=2)),
            "codex": _Adapter("codex", scanner.ScanResult(duplicates=1)),
        }

        with mock.patch("analytics.create_adapter", side_effect=lambda name, args: adapters[name]):
            code, payload, _ = self._run(
                "scan", "--platform", "claude", "--platform", "codex",
                "--platform", "claude", "--db", str(self.db_path),
            )

        self.assertEqual(0, code)
        self.assertEqual(["claude", "codex"], list(payload["platforms"]))
        self.assertEqual(2, payload["platforms"]["claude"]["result"]["inserted"])
        self.assertEqual(1, payload["platforms"]["codex"]["result"]["duplicates"])

    def test_scan_all_isolates_failure_and_marks_missing_platform(self):
        adapters = {
            "codex": _Adapter("codex", scanner.ScanResult(inserted=1)),
            "claude": _Adapter("claude", error=OSError("private detail")),
            "opencode": _Adapter("opencode", installed=False),
            "pi": _Adapter("pi"),
        }

        with mock.patch("analytics.create_adapter", side_effect=lambda name, args: adapters[name]):
            code, payload, _ = self._run("scan-all", "--db", str(self.db_path))

        self.assertEqual(1, code)
        self.assertEqual("ready", payload["platforms"]["codex"]["status"])
        self.assertEqual("partial", payload["platforms"]["claude"]["status"])
        self.assertEqual("OSError", payload["platforms"]["claude"]["error"])
        self.assertNotIn("private detail", json.dumps(payload))
        self.assertEqual("not_installed", payload["platforms"]["opencode"]["status"])
        self.assertEqual("ready", payload["platforms"]["pi"]["status"])

    def test_create_adapter_forwards_platform_specific_overrides(self):
        arguments = analytics._parser().parse_args(
            [
                "scan-all", "--db", str(self.db_path),
                "--codex-home", str(self.root / "codex"),
                "--claude-home", str(self.root / "claude"),
                "--opencode-config-dir", str(self.root / "opencode"),
                "--opencode-command", "custom-opencode",
                "--pi-home", str(self.root / "pi"),
                "--pi-session-dir", str(self.root / "pi-sessions"),
            ]
        )
        constructors = {
            name: mock.Mock(return_value=object()) for name in analytics.PLATFORMS
        }

        with mock.patch.dict(analytics.ADAPTER_CLASSES, constructors, clear=True):
            for platform in analytics.PLATFORMS:
                analytics.create_adapter(platform, arguments)

        constructors["codex"].assert_called_once_with(
            codex_home=arguments.codex_home, db_path=arguments.db
        )
        constructors["claude"].assert_called_once_with(
            claude_home=arguments.claude_home, db_path=arguments.db
        )
        constructors["opencode"].assert_called_once_with(
            config_dir=arguments.opencode_config_dir,
            command="custom-opencode",
            db_path=arguments.db,
        )
        constructors["pi"].assert_called_once_with(
            pi_home=arguments.pi_home,
            session_dir=arguments.pi_session_dir,
            db_path=arguments.db,
        )

    def test_report_routes_paths_to_existing_generator(self):
        output = self.root / "dashboard.html"
        with mock.patch("analytics.report.generate_report", return_value=output) as generate:
            code, payload, _ = self._run(
                "report", "--db", str(self.db_path), "--output", str(output),
                "--codex-home", str(self.root / "codex"),
            )

        self.assertEqual(0, code)
        self.assertEqual(str(output), payload["output"])
        generate.assert_called_once_with(
            self.db_path, output, codex_home=self.root / "codex"
        )

    def test_record_stores_supported_realtime_events_without_prompt_text(self):
        skill = self.root / "skills" / "pi-example" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: Pi Example\n---\n", encoding="utf-8")
        events = {
            "claude": {
                "hook_event_name": "PostToolUse", "tool_name": "Skill",
                "session_id": "claude-session", "tool_use_id": "claude-call",
                "tool_input": {"skill": "claude-example"},
                "prompt": "must not persist",
            },
            "opencode": {
                "tool": "skill", "sessionID": "opencode-session",
                "callID": "opencode-call", "args": {"name": "opencode-example"},
            },
            "pi": {
                "sessionId": "pi-session", "toolCallId": "pi-call",
                "toolName": "read", "args": {"path": str(skill)},
                "cwd": str(self.root), "provider": "vendor", "model": "model",
            },
        }

        for platform, event in events.items():
            with self.subTest(platform=platform):
                code, payload, _ = self._run(
                    "record", "--platform", platform, "--db", str(self.db_path),
                    stdin=json.dumps(event),
                )
                self.assertEqual(0, code)
                self.assertEqual(1, payload["result"]["inserted"])

        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT platform, skill_name, ingest_source FROM invocations ORDER BY platform"
            ).fetchall()
            schema = " ".join(
                row[1] for row in connection.execute("PRAGMA table_info(invocations)")
            )
        finally:
            connection.close()
        self.assertEqual(
            [
                ("claude", "claude-example", "realtime"),
                ("opencode", "opencode-example", "realtime"),
                ("pi", "Pi Example", "realtime"),
            ],
            rows,
        )
        self.assertNotIn("prompt", schema)

    def test_record_ignored_event_is_successful_and_does_not_write_invocation(self):
        code, payload, _ = self._run(
            "record", "--platform", "opencode", "--db", str(self.db_path),
            stdin=json.dumps({"tool": "read"}),
        )

        self.assertEqual(0, code)
        self.assertEqual("ignored", payload["status"])
        connection = scanner.init_db(self.db_path)
        try:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0])
        finally:
            connection.close()

    def test_claude_slash_record_requires_one_installed_skill(self):
        now = "2026-09-08T00:00:00Z"
        skill_path = str((self.root / "alpha" / "SKILL.md").resolve())
        scanner.store_installed_skills(
            [InstalledSkill("claude", skill_path, "alpha", skill_path, "user", now, now)],
            self.db_path,
        )
        event = {
            "hook_event_name": "UserPromptExpansion",
            "expansion_type": "slash_command",
            "session_id": "session",
            "prompt_id": "prompt",
            "command_name": "alpha",
        }
        transcript = self.root / "stable-id-session.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "user", "sessionId": "session", "uuid": "history-turn",
                "timestamp": now,
                "message": {"role": "user", "content": "<command-name>/alpha</command-name>"},
            }) + "\n",
            encoding="utf-8",
        )

        code, payload, _ = self._run(
            "record", "--platform", "claude", "--db", str(self.db_path),
            stdin=json.dumps({**event, "transcript_path": str(transcript)}),
        )
        ignored_code, ignored, _ = self._run(
            "record", "--platform", "claude", "--db", str(self.db_path),
            stdin=json.dumps({**event, "prompt_id": "other", "command_name": "not-a-skill"}),
        )

        self.assertEqual((0, "recorded"), (code, payload["status"]))
        self.assertEqual((0, "ignored"), (ignored_code, ignored["status"]))
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT skill_key, turn_id FROM invocations WHERE platform = 'claude'"
            ).fetchone()
            self.assertEqual(scanner.canonical_path(skill_path), row[0])
            self.assertEqual("history-turn", row[1])
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0])
        finally:
            connection.close()

    def test_claude_slash_without_stable_id_uses_transcript_or_records_diagnostic(self):
        now = "2026-09-08T00:00:00Z"
        skill = self.root / "alpha" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: alpha\n---\n", encoding="utf-8")
        skill_path = str(skill.resolve())
        scanner.store_installed_skills(
            [InstalledSkill("claude", skill_path, "alpha", skill_path, "user", now, now)],
            self.db_path,
        )
        event = {
            "hook_event_name": "UserPromptExpansion",
            "expansion_type": "slash_command",
            "session_id": "session",
            "command_name": "alpha",
            "cwd": str(self.root),
        }

        code, ignored, _ = self._run(
            "record", "--platform", "claude", "--db", str(self.db_path),
            stdin=json.dumps(event),
        )
        transcript = self.root / "session.jsonl"
        transcript.write_text(
            json.dumps({
                "type": "user", "sessionId": "session", "uuid": "turn-from-transcript",
                "cwd": str(self.root), "timestamp": now,
                "message": {"role": "user", "content": "<command-name>/alpha</command-name>"},
            }) + "\n",
            encoding="utf-8",
        )
        scanned_code, scanned, _ = self._run(
            "record", "--platform", "claude", "--db", str(self.db_path),
            "--claude-home", str(self.root / "claude"),
            stdin=json.dumps({**event, "transcript_path": str(transcript)}),
        )

        self.assertEqual((0, "ignored"), (code, ignored["status"]))
        self.assertEqual((0, "recorded"), (scanned_code, scanned["status"]))
        connection = sqlite3.connect(self.db_path)
        try:
            diagnostics = connection.execute(
                "SELECT category, detail FROM diagnostics WHERE platform = 'claude'"
            ).fetchall()
            turns = connection.execute(
                "SELECT turn_id FROM invocations WHERE platform = 'claude'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([("slash_transcript_pending", "slash event was ignored")], diagnostics)
        self.assertEqual([("turn-from-transcript",)], turns)

    def test_claude_slash_hook_then_history_scan_remains_one_invocation(self):
        home = self.root / "claude-home"
        skill = home / "skills" / "alpha" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: alpha\n---\n", encoding="utf-8")
        transcript = home / "projects" / "project" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        entry = {
            "type": "user", "sessionId": "session", "uuid": "history-only-id",
            "timestamp": "2026-09-08T00:00:00Z",
            "message": {"role": "user", "content": "<command-name>/alpha</command-name>"},
        }
        transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        now = "2026-09-08T00:00:00Z"
        skill_path = scanner.canonical_path(skill)
        scanner.store_installed_skills(
            [InstalledSkill("claude", skill_path, "alpha", skill_path, "user", now, now)],
            self.db_path,
        )
        event = {
            "hook_event_name": "UserPromptExpansion", "expansion_type": "slash_command",
            "session_id": "session", "prompt_id": "hook-id", "command_name": "alpha",
            "transcript_path": str(transcript), "prompt": "must not persist",
        }

        first_code, first, _ = self._run(
            "record", "--platform", "claude", "--db", str(self.db_path),
            "--claude-home", str(home), stdin=json.dumps(event),
        )
        scan_code, _, _ = self._run(
            "scan", "--platform", "claude", "--db", str(self.db_path),
            "--claude-home", str(home),
        )

        self.assertEqual((0, "recorded", 0), (first_code, first["status"], scan_code))
        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT turn_id FROM invocations WHERE platform = 'claude'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([("history-only-id",)], rows)

    def test_claude_slash_waits_for_lagging_transcript_then_backfill_records_once(self):
        home = self.root / "lag-home"
        skill = home / "skills" / "alpha" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: alpha\n---\n", encoding="utf-8")
        transcript = home / "projects" / "project" / "lag.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("", encoding="utf-8")
        now = "2026-09-08T00:00:00Z"
        skill_path = scanner.canonical_path(skill)
        scanner.store_installed_skills(
            [InstalledSkill("claude", skill_path, "alpha", skill_path, "user", now, now)],
            self.db_path,
        )
        event = {
            "hook_event_name": "UserPromptExpansion", "expansion_type": "slash_command",
            "session_id": "session", "prompt_id": "hook-id", "command_name": "alpha",
            "transcript_path": str(transcript),
        }

        code, result, _ = self._run(
            "record", "--platform", "claude", "--db", str(self.db_path),
            "--claude-home", str(home), stdin=json.dumps(event),
        )
        transcript.write_text(
            json.dumps({
                "type": "user", "sessionId": "session", "uuid": "later-history-id",
                "timestamp": now,
                "message": {"role": "user", "content": "<command-name>/alpha</command-name>"},
            }) + "\n",
            encoding="utf-8",
        )
        scan_code, _, _ = self._run(
            "scan", "--platform", "claude", "--db", str(self.db_path),
            "--claude-home", str(home),
        )

        self.assertEqual((0, "ignored", 0), (code, result["status"], scan_code))
        connection = sqlite3.connect(self.db_path)
        try:
            invocations = connection.execute(
                "SELECT turn_id FROM invocations WHERE platform = 'claude'"
            ).fetchall()
            categories = connection.execute(
                "SELECT category FROM diagnostics WHERE platform = 'claude'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([("later-history-id",)], invocations)
        self.assertIn(("slash_transcript_pending",), categories)

    def test_status_combines_resolved_roots_with_persisted_scan_state(self):
        connection = scanner.init_db(self.db_path)
        with connection:
            connection.execute(
                "INSERT INTO platform_status VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "codex", "partial", "/stored/codex", "2026-09-08T01:00:00Z",
                    None, "1", "format", "2026-09-08T01:00:01Z",
                ),
            )
            connection.execute(
                "INSERT INTO diagnostics "
                "(platform, source_path, byte_offset, category, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("codex", "source", 0, "unknown_entry", "shape", "2026-09-08T01:00:01Z"),
            )
        connection.close()
        adapters = {
            platform: _Adapter(platform, installed=platform != "pi")
            for platform in analytics.PLATFORMS
        }

        with mock.patch("analytics.create_adapter", side_effect=lambda name, args: adapters[name]):
            with mock.patch(
                "install_hook.integration_status",
                side_effect=lambda platform, **kwargs: "installed" if platform == "codex" else "not_installed",
                create=True,
            ):
                code, payload, _ = self._run("status", "--db", str(self.db_path))

        self.assertEqual(0, code)
        codex = payload["platforms"]["codex"]
        self.assertEqual("partial", codex["status"])
        self.assertEqual("2026-09-08T01:00:00Z", codex["last_history_scan_at"])
        self.assertEqual(str(adapters["codex"].resolved_root), codex["resolved_root"])
        self.assertEqual("installed", codex["integration_status"])
        self.assertEqual([{"category": "unknown_entry", "count": 1}], codex["diagnostics"])
        self.assertEqual("not_installed", payload["platforms"]["pi"]["status"])

    def test_install_requires_explicit_platform_and_routes_installer_api(self):
        code, payload, _ = self._run("install", "--db", str(self.db_path))
        self.assertEqual(2, code)
        self.assertEqual("platform_required", payload["error"])

        installed = {"status": "installed", "path": str(self.root / "settings.json")}
        with mock.patch("install_hook.install_platform", return_value=installed, create=True) as install:
            code, payload, _ = self._run(
                "install", "--platform", "claude",
                "--claude-home", str(self.root / "claude"),
                "--db", str(self.db_path),
            )

        self.assertEqual(0, code)
        self.assertEqual(
            {
                **installed,
                "platform": "claude",
                "action": "install",
                "changed": True,
            },
            payload["platforms"]["claude"],
        )
        install.assert_called_once_with(
            "claude", codex_home=None, claude_home=self.root / "claude",
            opencode_config_dir=None, pi_home=None, db_path=self.db_path,
            output_path=analytics.report.DEFAULT_OUTPUT_PATH,
        )

    def test_uninstall_isolates_platform_failures(self):
        def uninstall(platform, **kwargs):
            if platform == "pi":
                raise ValueError("do not expose")
            return {"platform": platform, "action": "uninstall", "changed": False}

        with mock.patch("install_hook.uninstall_platform", side_effect=uninstall, create=True):
            code, payload, _ = self._run(
                "uninstall", "--platform", "codex", "--platform", "pi",
                "--db", str(self.db_path),
            )

        self.assertEqual(1, code)
        self.assertFalse(payload["platforms"]["codex"]["changed"])
        self.assertEqual("ValueError", payload["platforms"]["pi"]["error"])
        self.assertNotIn("do not expose", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
