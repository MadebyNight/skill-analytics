import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

import analytics
import install_hook
import scanner
from adapters import ClaudeAdapter, CodexAdapter, OpenCodeAdapter, PiAdapter


class MultiPlatformEndToEndTests(unittest.TestCase):
    def test_isolated_scan_report_install_realtime_report_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            homes = {name: root / "homes" / name for name in analytics.ADAPTER_CLASSES}
            project = root / "project"
            fake_user = root / "user"
            db = root / "output" / "analytics.db"
            dashboard = root / "output" / "dashboard.html"
            for path in (*homes.values(), project, fake_user):
                path.mkdir(parents=True)
            skills = {
                name: home / "skills" / "alpha" / "SKILL.md"
                for name, home in homes.items()
            }
            for path in skills.values():
                path.parent.mkdir(parents=True)
                path.write_text("---\nname: alpha\n---\n", encoding="utf-8")

            codex_transcript = homes["codex"] / "sessions" / "session.jsonl"
            codex_transcript.parent.mkdir()
            codex_transcript.write_text(
                json.dumps(self._codex_event("codex-history", skills["codex"])) + "\n",
                encoding="utf-8",
            )
            claude_transcript = homes["claude"] / "projects" / "fixture" / "session.jsonl"
            claude_transcript.parent.mkdir(parents=True)
            claude_transcript.write_text(
                json.dumps({
                    "type": "assistant", "sessionId": "claude-history",
                    "uuid": "claude-turn", "cwd": str(project),
                    "timestamp": "2026-09-08T01:00:00Z",
                    "message": {"role": "assistant", "model": "fixture",
                                "content": [{"type": "tool_use", "id": "tool-alpha",
                                             "name": "Skill", "input": {"skill": "alpha"}}]},
                }) + "\n",
                encoding="utf-8",
            )
            pi_transcript = homes["pi"] / "sessions" / "session.jsonl"
            pi_transcript.parent.mkdir()
            pi_transcript.write_text(
                "\n".join(json.dumps(item) for item in [
                    {"type": "session", "version": 3, "id": "pi-history",
                     "timestamp": "2026-09-08T02:00:00Z", "cwd": str(project)},
                    {"type": "message", "id": "assistant", "parentId": None,
                     "timestamp": "2026-09-08T02:00:01Z",
                     "message": {"role": "assistant", "provider": "fixture",
                                 "model": "fixture", "content": [{"type": "toolCall",
                                 "id": "pi-call", "name": "read",
                                 "arguments": {"path": str(skills["pi"])}}]}},
                    {"type": "message", "id": "result", "parentId": "assistant",
                     "timestamp": "2026-09-08T02:00:02Z",
                     "message": {"role": "toolResult", "toolCallId": "pi-call",
                                 "toolName": "read", "content": [], "isError": False}},
                ]) + "\n",
                encoding="utf-8",
            )
            opencode_export = {
                "info": {"id": "opencode-history", "directory": str(project),
                         "time": {"created": 1788825600000}},
                "messages": [{"info": {"id": "message", "role": "assistant",
                                           "time": {"created": 1788825601000}},
                              "parts": [{"type": "tool", "callID": "oc-call",
                                         "tool": "skill", "state": {
                                             "status": "completed",
                                             "input": {"name": "alpha"}}}]}],
            }
            opencode_calls = []

            def create_adapter(platform, _args):
                if platform == "codex":
                    return CodexAdapter(codex_home=homes[platform],
                                        agents_home=fake_user / ".agents",
                                        plugin_root=root / "empty-plugins", db_path=db)
                if platform == "claude":
                    return ClaudeAdapter(claude_home=homes[platform],
                                         project_dirs=[project], db_path=db)
                if platform == "pi":
                    return PiAdapter(pi_home=homes[platform],
                                     session_dir=homes[platform] / "sessions",
                                     cwd=project, home=fake_user, db_path=db)
                adapter = OpenCodeAdapter(config_dir=homes[platform], command="never-run",
                                          cwd=project, home=fake_user, db_path=db)

                def fake_run(arguments):
                    opencode_calls.append(arguments)
                    stdout = (
                        json.dumps([{"id": "opencode-history"}])
                        if arguments[:2] == ["session", "list"]
                        else json.dumps(opencode_export)
                    )
                    return subprocess.CompletedProcess(arguments, 0, stdout, "")

                adapter._run = fake_run
                return adapter

            common = [
                "--codex-home", str(homes["codex"]),
                "--claude-home", str(homes["claude"]),
                "--opencode-config-dir", str(homes["opencode"]),
                "--pi-home", str(homes["pi"]),
                "--pi-session-dir", str(homes["pi"] / "sessions"),
                "--db", str(db), "--output", str(dashboard),
            ]
            environment = {
                "CODEX_HOME": str(homes["codex"]),
                "CLAUDE_CONFIG_DIR": str(homes["claude"]),
                "OPENCODE_CONFIG_DIR": str(homes["opencode"]),
                "PI_CODING_AGENT_DIR": str(homes["pi"]),
                "PI_CODING_AGENT_SESSION_DIR": str(homes["pi"] / "sessions"),
            }
            with mock.patch.dict(os.environ, environment), \
                 mock.patch("pathlib.Path.home", return_value=fake_user), \
                 mock.patch("analytics.create_adapter", side_effect=create_adapter), \
                 mock.patch("adapters.opencode.subprocess.run",
                            side_effect=AssertionError("external OpenCode CLI invoked")), \
                 mock.patch("report._refresh_codex_inventory"):
                self.assertEqual(0, self._main(["scan-all", *common]))
                self.assertEqual(4, self._invocation_count(db))
                self.assertEqual(0, self._main(["scan-all", *common]))
                self.assertEqual(4, self._invocation_count(db))
                self.assertEqual(0, self._main(["report", *common]))
                self.assertTrue(dashboard.is_file())

                platforms = [item for name in homes for item in ("--platform", name)]
                self.assertEqual(0, self._main(["install", *platforms, *common]))
                for name in homes:
                    self.assertEqual("installed", install_hook.integration_status(
                        name, codex_home=homes["codex"], claude_home=homes["claude"],
                        opencode_config_dir=homes["opencode"], pi_home=homes["pi"]))

                with codex_transcript.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(
                        self._codex_event("codex-realtime", skills["codex"])) + "\n")
                scanner.scan_transcript(codex_transcript, db, ingest_source="hook")
                events = {
                    "claude": {"hook_event_name": "PostToolUse",
                               "session_id": "claude-realtime", "tool_use_id": "call",
                               "tool_name": "Skill", "tool_input": {"skill": "alpha"}},
                    "opencode": {"tool": "skill", "sessionID": "oc-realtime",
                                 "callID": "call", "args": {"name": "alpha"}},
                    "pi": {"toolName": "read", "sessionId": "pi-realtime",
                           "toolCallId": "call", "args": {"path": str(skills["pi"])}},
                }
                for platform, payload in events.items():
                    with mock.patch("analytics.sys.stdin",
                                    io.StringIO(json.dumps(payload))):
                        self.assertEqual(
                            0, self._main(["record", "--platform", platform, *common])
                        )
                self.assertEqual(8, self._invocation_count(db))
                self.assertEqual(0, self._main(["report", *common]))
                self.assertIn('id="dashboard-data"',
                              dashboard.read_text(encoding="utf-8"))

                self.assertEqual(0, self._main(["uninstall", *platforms, *common]))
                for name in homes:
                    self.assertEqual("not_installed", install_hook.integration_status(
                        name, codex_home=homes["codex"], claude_home=homes["claude"],
                        opencode_config_dir=homes["opencode"], pi_home=homes["pi"]))
                self.assertTrue(opencode_calls)
                self.assertTrue(all(path.is_relative_to(root) for path in (
                    db, dashboard, homes["codex"] / "hooks.json",
                    homes["claude"] / "settings.json")))

    @staticmethod
    def _main(arguments):
        with redirect_stdout(io.StringIO()):
            return analytics.main(arguments)

    @staticmethod
    def _invocation_count(db):
        with closing(sqlite3.connect(db)) as connection:
            return connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]

    @staticmethod
    def _codex_event(turn_id, skill_path):
        command = json.dumps(f"Get-Content -Raw '{skill_path}'")
        return {
            "timestamp": "2026-09-08T00:00:00Z", "session_id": "codex-session",
            "type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec",
                "input": ("const r = await tools.exec_command({cmd: " + command +
                          "}); text(r.output);"),
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            },
        }


if __name__ == "__main__":
    unittest.main()
