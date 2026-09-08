import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from adapters.opencode import OpenCodeAdapter, _resolve_command, extract_invocations


FIXTURE = Path(__file__).parent / "fixtures" / "opencode" / "export-session.json"
PLUGIN = Path(__file__).parents[1] / "integrations" / "opencode-skill-analytics.js"


class OpenCodeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.config = self.root / "explicit-config"
        self.project = self.root / "repo"
        self.cwd = self.project / "nested" / "work"
        self.cwd.mkdir(parents=True)
        (self.project / ".git").mkdir()
        self.db_path = self.root / "analytics.db"

    @staticmethod
    def _write_skill(root, name):
        path = root / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: fixture\n---\n",
            encoding="utf-8",
        )
        return path.resolve()

    def test_config_precedence_is_explicit_then_environment_then_default(self):
        environment = self.root / "environment-config"
        with mock.patch.dict(os.environ, {"OPENCODE_CONFIG_DIR": str(environment)}):
            explicit = OpenCodeAdapter(config_dir=self.config, home=self.home)
            from_environment = OpenCodeAdapter(home=self.home)
        with mock.patch.dict(os.environ, {}, clear=True):
            default = OpenCodeAdapter(home=self.home)

        self.assertEqual(self.config.resolve(), explicit.resolved_root)
        self.assertEqual(environment.resolve(), from_environment.resolved_root)
        self.assertEqual((self.home / ".config" / "opencode").resolve(), default.resolved_root)

    def test_discovers_six_official_global_and_ancestor_skill_locations(self):
        expected = {
            self._write_skill(self.config, "global-opencode"),
            self._write_skill(self.home / ".claude", "global-claude"),
            self._write_skill(self.home / ".agents", "global-agents"),
            self._write_skill(self.project / ".opencode", "project-opencode"),
            self._write_skill(self.project / ".claude", "project-claude"),
            self._write_skill(self.project / ".agents", "project-agents"),
            self._write_skill(self.project / "nested" / ".opencode", "ancestor-opencode"),
        }
        outside = self._write_skill(self.root / ".opencode", "outside-worktree")

        skills = OpenCodeAdapter(
            config_dir=self.config,
            home=self.home,
            cwd=self.cwd,
        ).discover_installed_skills()

        actual = {Path(skill.skill_path) for skill in skills}
        self.assertEqual(expected, actual)
        self.assertNotIn(outside, actual)
        self.assertEqual("opencode", {skill.platform for skill in skills}.pop())

    def test_extracts_completed_official_tool_parts_and_ignores_unknown_shapes(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

        invocations = extract_invocations(payload)

        self.assertEqual(["alpha", "beta"], [item.skill_name for item in invocations])
        self.assertEqual(["call_alpha", "call_beta"], [item.turn_id for item in invocations])
        self.assertEqual({"structured_skill"}, {item.evidence_type for item in invocations})
        self.assertEqual({"subagent"}, {item.agent_kind for item in invocations})
        self.assertEqual({"provider-test/model-test"}, {item.model for item in invocations})
        self.assertEqual({"/workspace/project/subdir"}, {item.cwd for item in invocations})
        self.assertEqual("2026-09-08T00:00:02Z", invocations[0].invoked_at)

    def test_windows_bare_command_resolves_launcher_but_explicit_path_is_preserved(self):
        with mock.patch("adapters.opencode.os.name", "nt"), mock.patch(
            "adapters.opencode.shutil.which", return_value=r"C:\tools\opencode.CMD"
        ) as which:
            self.assertEqual(r"C:\tools\opencode.CMD", _resolve_command("opencode"))
            which.assert_called_once_with("opencode")

        with mock.patch("adapters.opencode.os.name", "nt"), mock.patch(
            "adapters.opencode.shutil.which"
        ) as which:
            explicit = r"C:\portable\opencode.exe"
            self.assertEqual(explicit, _resolve_command(explicit))
            which.assert_not_called()

    @mock.patch("adapters.opencode.subprocess.run")
    def test_scan_uses_only_public_cli_and_isolates_one_failed_export(self, run):
        fixture_text = FIXTURE.read_text(encoding="utf-8")
        run.side_effect = [
            subprocess.CompletedProcess(
                [], 0, json.dumps([{"id": "ses_fixture"}, {"id": "ses_bad"}, {"id": "ses_empty"}]), ""
            ),
            subprocess.CompletedProcess([], 0, fixture_text, ""),
            subprocess.CompletedProcess([], 2, "", "broken"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        adapter = OpenCodeAdapter(
            config_dir=self.config,
            home=self.home,
            cwd=self.cwd,
            command="opencode-test",
            db_path=self.db_path,
        )

        result = adapter.scan()

        self.assertEqual(3, result.files)
        self.assertEqual(2, result.inserted)
        self.assertEqual(1, result.failures)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            [
                ["opencode-test", "session", "list", "--format", "json"],
                ["opencode-test", "export", "ses_fixture"],
                ["opencode-test", "export", "ses_bad"],
                ["opencode-test", "export", "ses_empty"],
            ],
            commands,
        )
        for call in run.call_args_list:
            self.assertFalse(call.kwargs["shell"])
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertEqual(self.cwd, call.kwargs["cwd"])
            self.assertEqual(str(self.config.resolve()), call.kwargs["env"]["OPENCODE_CONFIG_DIR"])

        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT skill_name, ingest_source FROM invocations ORDER BY skill_name"
            ).fetchall()
            status = connection.execute(
                "SELECT status, format_version FROM platform_status WHERE platform = 'opencode'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual([("alpha", "history"), ("beta", "history")], rows)
        self.assertEqual(("partial", "cli-export/tool-part-v1"), status)

    @mock.patch("adapters.opencode.subprocess.run")
    def test_empty_session_list_stdout_is_a_successful_empty_scan(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        result = OpenCodeAdapter(
            config_dir=self.config,
            home=self.home,
            cwd=self.cwd,
            command="opencode-test",
            db_path=self.db_path,
        ).scan()

        self.assertEqual(0, result.failures)
        self.assertEqual(0, result.parse_errors)
        connection = sqlite3.connect(self.db_path)
        try:
            status = connection.execute(
                "SELECT status FROM platform_status WHERE platform = 'opencode'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual("ready", status)

    @mock.patch("adapters.opencode.subprocess.run")
    def test_unknown_export_object_is_unsupported_and_diagnostic_is_redacted(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, json.dumps([{"id": "ses_unknown"}]), ""),
            subprocess.CompletedProcess(
                [], 0, json.dumps({"privateField": {"secret": "must-not-be-stored"}}), ""
            ),
        ]

        result = OpenCodeAdapter(
            config_dir=self.config,
            home=self.home,
            cwd=self.cwd,
            command="opencode-test",
            db_path=self.db_path,
        ).scan()

        self.assertEqual(1, result.parse_errors)
        connection = sqlite3.connect(self.db_path)
        try:
            status = connection.execute(
                "SELECT status FROM platform_status WHERE platform='opencode'"
            ).fetchone()[0]
            category, detail = connection.execute(
                "SELECT category, detail FROM diagnostics WHERE platform='opencode'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual("unsupported_version", status)
        self.assertEqual("unsupported_version", category)
        self.assertNotIn("secret", detail)
        self.assertNotIn("privateField", detail)
        self.assertNotIn("must-not-be-stored", detail)

    @mock.patch("adapters.opencode.subprocess.run")
    def test_non_object_exports_are_unsupported_and_diagnosed(self, run):
        for index, payload in enumerate(([], "future-schema", None)):
            with self.subTest(payload=payload):
                db_path = self.root / f"non-object-{index}.db"
                run.side_effect = [
                    subprocess.CompletedProcess([], 0, json.dumps([{"id": "ses_unknown"}]), ""),
                    subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
                ]

                result = OpenCodeAdapter(
                    config_dir=self.config,
                    home=self.home,
                    cwd=self.cwd,
                    command="opencode-test",
                    db_path=db_path,
                ).scan()

                self.assertEqual(1, result.parse_errors)
                connection = sqlite3.connect(db_path)
                try:
                    status = connection.execute(
                        "SELECT status FROM platform_status WHERE platform='opencode'"
                    ).fetchone()[0]
                    category, detail = connection.execute(
                        "SELECT category, detail FROM diagnostics WHERE platform='opencode'"
                    ).fetchone()
                finally:
                    connection.close()
                self.assertEqual("unsupported_version", status)
                self.assertEqual("unsupported_version", category)
                self.assertEqual("OpenCode export schema is not supported", detail)

    @mock.patch("adapters.opencode.subprocess.run")
    def test_valid_and_non_object_exports_are_partial(self, run):
        valid = {
            "info": {"id": "ses_valid", "directory": str(self.cwd), "time": {"created": 1}},
            "messages": [],
        }
        run.side_effect = [
            subprocess.CompletedProcess(
                [], 0, json.dumps([{"id": "ses_valid"}, {"id": "ses_unknown"}]), ""
            ),
            subprocess.CompletedProcess([], 0, json.dumps(valid), ""),
            subprocess.CompletedProcess([], 0, "null", ""),
        ]

        result = OpenCodeAdapter(
            config_dir=self.config,
            home=self.home,
            cwd=self.cwd,
            command="opencode-test",
            db_path=self.db_path,
        ).scan()

        self.assertEqual(1, result.parse_errors)
        connection = sqlite3.connect(self.db_path)
        try:
            status = connection.execute(
                "SELECT status FROM platform_status WHERE platform='opencode'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual("partial", status)

    @mock.patch("adapters.opencode.subprocess.run")
    def test_recognized_export_with_unknown_part_is_partial_but_keeps_invocations(self, run):
        run.side_effect = [
            subprocess.CompletedProcess([], 0, json.dumps([{"id": "ses_fixture"}]), ""),
            subprocess.CompletedProcess([], 0, FIXTURE.read_text(encoding="utf-8"), ""),
        ]

        result = OpenCodeAdapter(
            config_dir=self.config,
            home=self.home,
            cwd=self.cwd,
            command="opencode-test",
            db_path=self.db_path,
        ).scan()

        self.assertEqual(2, result.inserted)
        self.assertEqual(1, result.parse_errors)
        connection = sqlite3.connect(self.db_path)
        try:
            status = connection.execute(
                "SELECT status FROM platform_status WHERE platform='opencode'"
            ).fetchone()[0]
            category = connection.execute(
                "SELECT category FROM diagnostics WHERE platform='opencode'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual("partial", status)
        self.assertEqual("unsupported_version", category)

    @mock.patch("adapters.opencode.subprocess.run")
    def test_recognized_export_without_skill_calls_remains_ready(self, run):
        payload = {
            "info": {"id": "ses_text", "directory": str(self.cwd), "time": {"created": 1}},
            "messages": [
                {
                    "info": {"id": "msg_text", "role": "assistant", "time": {"created": 1}},
                    "parts": [{"id": "prt_text", "type": "text", "text": "hello"}],
                }
            ],
        }
        run.side_effect = [
            subprocess.CompletedProcess([], 0, json.dumps([{"id": "ses_text"}]), ""),
            subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
        ]

        result = OpenCodeAdapter(
            config_dir=self.config,
            home=self.home,
            cwd=self.cwd,
            command="opencode-test",
            db_path=self.db_path,
        ).scan()

        self.assertEqual(0, result.parse_errors)
        connection = sqlite3.connect(self.db_path)
        try:
            status = connection.execute(
                "SELECT status FROM platform_status WHERE platform='opencode'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual("ready", status)

    @mock.patch("adapters.opencode.subprocess.run", side_effect=FileNotFoundError)
    def test_missing_command_reports_not_installed(self, run):
        result = OpenCodeAdapter(
            config_dir=self.config,
            home=self.home,
            cwd=self.cwd,
            command="missing-opencode",
            db_path=self.db_path,
        ).scan()

        self.assertEqual(0, result.failures)
        connection = sqlite3.connect(self.db_path)
        try:
            status = connection.execute(
                "SELECT status FROM platform_status WHERE platform = 'opencode'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual("not_installed", status)


class OpenCodePluginTests(unittest.TestCase):
    def test_plugin_has_the_nonblocking_official_hook_contract(self):
        source = PLUGIN.read_text(encoding="utf-8")
        self.assertIn('"tool.execute.after"', source)
        self.assertIn('input.tool !== "skill"', source)
        self.assertIn("input.sessionID", source)
        self.assertIn("input.callID", source)
        self.assertIn("input.args.name", source)
        self.assertIn('shell: false', source)
        self.assertIn('detached: true', source)
        self.assertIn('stdio: ["pipe", "ignore", "ignore"]', source)
        self.assertIn("child.unref()", source)
        self.assertNotIn("await child", source)

    @unittest.skipUnless(os.name == "nt" or Path("/usr/bin/env").exists(), "requires a local Node runtime")
    def test_plugin_writes_event_json_to_record_process_stdin(self):
        node = "node.exe" if os.name == "nt" else "node"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analytics = root / "analytics.py"
            output = root / "event.json"
            analytics.write_text(
                "import pathlib, sys\n"
                f"pathlib.Path({str(output)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n",
                encoding="utf-8",
            )
            source = PLUGIN.read_text(encoding="utf-8")
            source = source.replace("__PYTHON_EXECUTABLE__", json.dumps(sys.executable))
            source = source.replace("__PROJECT_ROOT__", json.dumps(str(root)))
            source = source.replace("__DATABASE_PATH__", json.dumps(str(root / "events.sqlite")))
            module = root / "plugin.mjs"
            module.write_text(source, encoding="utf-8")
            runner = (
                f"import({json.dumps(module.as_uri())}).then(async m => {{"
                "const hooks = await m.SkillAnalyticsPlugin();"
                "await hooks['tool.execute.after']({tool:'skill',sessionID:'ses-1',"
                "callID:'call-1',args:{name:'alpha'}},{});"
                "})"
            )

            completed = subprocess.run(
                [node, "--input-type=module", "-e", runner],
                capture_output=True,
                text=True,
                timeout=5,
                shell=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            deadline = time.monotonic() + 5
            while not output.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(output.exists())
            self.assertEqual(
                {
                    "tool": "skill",
                    "sessionID": "ses-1",
                    "callID": "call-1",
                    "args": {"name": "alpha"},
                },
                json.loads(output.read_text(encoding="utf-8")),
            )


if __name__ == "__main__":
    unittest.main()
