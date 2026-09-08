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

import scanner
from adapters.base import canonical_path
from adapters.pi import PiAdapter, parse_realtime_event


FIXTURES = Path(__file__).parent / "fixtures" / "pi"
EXTENSION = Path(__file__).parents[1] / "integrations" / "pi-skill-analytics.ts"


class PiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.agent_dir = self.root / "pi-agent"
        self.project = self.root / "repo"
        self.cwd = self.project / "nested" / "work"
        self.cwd.mkdir(parents=True)
        (self.project / ".git").mkdir()
        self.db_path = self.root / "analytics.db"

    @staticmethod
    def _write_skill(root: Path, name: str) -> Path:
        path = root / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: fixture\n---\n", encoding="utf-8"
        )
        return path.resolve()

    def _copy_session(self, fixture: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        source = (FIXTURES / fixture).read_text(encoding="utf-8")
        target.write_text(source.replace("__CWD__", str(self.project).replace("\\", "\\\\")), encoding="utf-8")
        return target

    def _rows(self, query: str):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(query).fetchall()
        finally:
            connection.close()

    def test_agent_and_session_directory_precedence(self):
        configured = self.cwd / "configured-sessions"
        self.agent_dir.mkdir(parents=True)
        (self.agent_dir / "settings.json").write_text(
            json.dumps({"sessionDir": "configured-sessions"}), encoding="utf-8"
        )
        environment_agent = self.root / "environment-agent"
        environment_sessions = self.root / "environment-sessions"
        explicit_sessions = self.root / "explicit-sessions"

        with mock.patch.dict(
            os.environ,
            {
                "PI_CODING_AGENT_DIR": str(environment_agent),
                "PI_CODING_AGENT_SESSION_DIR": str(environment_sessions),
            },
        ):
            explicit = PiAdapter(
                pi_home=self.agent_dir,
                session_dir=explicit_sessions,
                home=self.home,
            )
            from_environment = PiAdapter(pi_home=self.agent_dir, home=self.home)
            environment_root = PiAdapter(home=self.home)
        with mock.patch.dict(os.environ, {}, clear=True):
            from_settings = PiAdapter(pi_home=self.agent_dir, home=self.home, cwd=self.cwd)
            default = PiAdapter(pi_home=self.root / "empty-agent", home=self.home)

        self.assertEqual(self.agent_dir.resolve(), explicit.resolved_root)
        self.assertEqual(environment_agent.resolve(), environment_root.resolved_root)
        self.assertEqual(explicit_sessions.resolve(), explicit.resolved_session_dir)
        self.assertEqual(environment_sessions.resolve(), from_environment.resolved_session_dir)
        self.assertEqual(configured.resolve(), from_settings.resolved_session_dir)
        self.assertEqual((self.root / "empty-agent" / "sessions").resolve(), default.resolved_session_dir)

    def test_discovers_global_ancestor_settings_package_and_explicit_skills(self):
        global_pi = self._write_skill(self.agent_dir / "skills", "global-pi")
        global_agents = self._write_skill(self.home / ".agents" / "skills", "global-agents")
        project_pi = self._write_skill(self.project / ".pi" / "skills", "project-pi")
        ancestor_agents = self._write_skill(
            self.project / "nested" / ".agents" / "skills", "ancestor-agents"
        )
        outside = self._write_skill(self.root / ".pi" / "skills", "outside")
        configured = self._write_skill(self.agent_dir / "configured", "configured")
        package = self._write_skill(self.agent_dir / "package" / "skills", "packaged")
        (self.agent_dir / "package" / "package.json").write_text(
            json.dumps({"pi": {"skills": ["skills"]}}), encoding="utf-8"
        )
        npm_package_root = self.agent_dir / "npm" / "node_modules" / "pi-package"
        npm_package = self._write_skill(npm_package_root / "skills", "npm-packaged")
        (npm_package_root / "package.json").write_text(
            json.dumps({"pi": {"skills": ["skills"]}}), encoding="utf-8"
        )
        explicit = self._write_skill(self.root / "explicit", "explicit")
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_dir / "settings.json").write_text(
            json.dumps(
                {"skills": ["configured"], "packages": ["./package", "npm:pi-package@1.0.0"]}
            ),
            encoding="utf-8",
        )

        skills = PiAdapter(
            pi_home=self.agent_dir,
            home=self.home,
            cwd=self.cwd,
            skill_dirs=[explicit],
        ).discover_installed_skills()

        actual = {Path(skill.skill_path) for skill in skills}
        self.assertEqual(
            {
                global_pi, global_agents, project_pi, ancestor_agents, configured,
                package, npm_package, explicit,
            },
            actual,
        )
        self.assertNotIn(outside, actual)
        sources = {skill.skill_name: skill.skill_source for skill in skills}
        self.assertEqual("package", sources["packaged"])
        self.assertEqual("configured", sources["configured"])
        self.assertEqual("explicit", sources["explicit"])

    def test_scan_requires_successful_descendant_tool_result_and_supports_legacy_header(self):
        self._write_skill(self.project / "skills", "alpha")
        self._write_skill(self.project / "skills", "beta")
        session_dir = self.agent_dir / "sessions"
        self._copy_session("session-v3.jsonl", session_dir / "v3.jsonl")
        self._copy_session("session-legacy.jsonl", session_dir / "legacy.jsonl")

        result = PiAdapter(
            pi_home=self.agent_dir,
            session_dir=session_dir,
            home=self.home,
            cwd=self.project,
            skill_dirs=[self.project / "skills"],
            db_path=self.db_path,
        ).scan()

        self.assertEqual(2, result.files)
        self.assertEqual(2, result.inserted)
        self.assertEqual(0, result.parse_errors)
        rows = self._rows(
            "SELECT session_id, turn_id, skill_name, skill_path, evidence_type, "
            "agent_kind, model, cwd, invoked_at FROM invocations "
            "WHERE platform = 'pi' ORDER BY invoked_at"
        )
        self.assertEqual(
            [
                ("pi-v3", "call-alpha", "alpha", "provider-test/model-test"),
                ("pi-legacy", "call-beta", "beta", "legacy-provider/legacy-model"),
            ],
            [(r["session_id"], r["turn_id"], r["skill_name"], r["model"]) for r in rows],
        )
        self.assertEqual({"skill_file_read"}, {r["evidence_type"] for r in rows})
        self.assertEqual({"main"}, {r["agent_kind"] for r in rows})
        self.assertEqual(canonical_path(self.project), canonical_path(rows[0]["cwd"]))
        self.assertEqual(canonical_path(self.project / "skills" / "alpha" / "SKILL.md"), rows[0]["skill_path"])
        self.assertEqual("2026-09-08T03:00:02Z", rows[0]["invoked_at"])
        self.assertEqual("ready", self._rows("SELECT status FROM platform_status WHERE platform='pi'")[0][0])

        repeated = PiAdapter(
            pi_home=self.agent_dir,
            session_dir=session_dir,
            home=self.home,
            cwd=self.project,
            skill_dirs=[self.project / "skills"],
            db_path=self.db_path,
        ).scan()
        self.assertEqual(0, repeated.inserted)
        self.assertEqual(0, repeated.duplicates)
        self.assertEqual(0, repeated.lines)

    def test_incremental_scan_correlates_an_appended_result_with_an_existing_call(self):
        skill = self._write_skill(self.project / "skills", "alpha")
        session_dir = self.agent_dir / "sessions"
        transcript = session_dir / "append.jsonl"
        transcript.parent.mkdir(parents=True)
        entries = [
            {
                "type": "session", "version": 3, "id": "append-session",
                "timestamp": "2026-09-08T06:00:00Z", "cwd": str(self.project),
            },
            {
                "type": "message", "id": "assistant", "parentId": None,
                "timestamp": "2026-09-08T06:00:01Z",
                "message": {"role": "assistant", "provider": "test", "model": "model",
                            "content": [{"type": "toolCall", "id": "append-call", "name": "read",
                                         "arguments": {"path": str(skill)}}]},
            },
        ]
        transcript.write_text("".join(json.dumps(item) + "\n" for item in entries), encoding="utf-8")
        adapter = PiAdapter(
            pi_home=self.agent_dir, session_dir=session_dir, home=self.home,
            cwd=self.project, db_path=self.db_path,
        )
        self.assertEqual(0, adapter.scan().inserted)

        result = {
            "type": "message", "id": "result", "parentId": "assistant",
            "timestamp": "2026-09-08T06:00:02Z",
            "message": {"role": "toolResult", "toolCallId": "append-call",
                        "toolName": "read", "content": [], "isError": False},
        }
        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result) + "\n")

        scanned = adapter.scan()
        self.assertEqual(1, scanned.lines)
        self.assertEqual(1, scanned.inserted)
        self.assertEqual(0, scanned.duplicates)

    def test_unknown_session_version_is_diagnosed_and_marks_unsupported(self):
        session_dir = self.agent_dir / "sessions"
        self._copy_session("session-unknown.jsonl", session_dir / "future.jsonl")

        result = PiAdapter(
            pi_home=self.agent_dir,
            session_dir=session_dir,
            home=self.home,
            cwd=self.project,
            db_path=self.db_path,
        ).scan()

        self.assertEqual(1, result.files)
        self.assertEqual(0, result.inserted)
        self.assertEqual(1, result.parse_errors)
        self.assertEqual(
            "unsupported_version",
            self._rows("SELECT status FROM platform_status WHERE platform='pi'")[0][0],
        )
        self.assertEqual(
            "unsupported_version",
            self._rows("SELECT category FROM diagnostics WHERE platform='pi'")[0][0],
        )
        PiAdapter(
            pi_home=self.agent_dir,
            session_dir=session_dir,
            home=self.home,
            cwd=self.project,
            db_path=self.db_path,
        ).scan()
        self.assertEqual(
            "unsupported_version",
            self._rows("SELECT status FROM platform_status WHERE platform='pi'")[0][0],
        )

    def test_unknown_version_mixed_with_other_parse_error_marks_partial(self):
        session_dir = self.agent_dir / "sessions"
        self._copy_session("session-unknown.jsonl", session_dir / "future.jsonl")
        malformed = session_dir / "malformed.jsonl"
        malformed.write_text(
            json.dumps(
                {
                    "type": "session",
                    "version": 3,
                    "id": "malformed",
                    "cwd": str(self.project),
                }
            )
            + "\n{invalid\n",
            encoding="utf-8",
        )

        result = PiAdapter(
            pi_home=self.agent_dir,
            session_dir=session_dir,
            home=self.home,
            cwd=self.project,
            db_path=self.db_path,
        ).scan()

        self.assertEqual(2, result.parse_errors)
        self.assertEqual(
            "partial",
            self._rows("SELECT status FROM platform_status WHERE platform='pi'")[0][0],
        )
        PiAdapter(
            pi_home=self.agent_dir,
            session_dir=session_dir,
            home=self.home,
            cwd=self.project,
            db_path=self.db_path,
        ).scan()
        self.assertEqual(
            "partial",
            self._rows("SELECT status FROM platform_status WHERE platform='pi'")[0][0],
        )

    def test_missing_agent_directory_is_not_installed(self):
        result = PiAdapter(
            pi_home=self.root / "missing",
            home=self.home,
            cwd=self.project,
            db_path=self.db_path,
        ).scan()
        self.assertEqual(scanner.ScanResult(), result)
        self.assertEqual("not_installed", self._rows("SELECT status FROM platform_status WHERE platform='pi'")[0][0])

    def test_realtime_event_parser_accepts_only_read_skill_files(self):
        skill = self._write_skill(self.project / "skills", "alpha")
        event = {
            "sessionId": "live-session", "toolCallId": "live-call", "toolName": "read",
            "args": {"path": str(skill)}, "cwd": str(self.project),
            "provider": "live-provider", "model": "live-model",
            "timestamp": "2026-09-08T07:00:00Z",
        }
        invocation = parse_realtime_event(event)
        self.assertIsNotNone(invocation)
        self.assertEqual("live-call", invocation.turn_id)
        self.assertEqual("alpha", invocation.skill_name)
        self.assertEqual("live-provider/live-model", invocation.model)
        self.assertEqual("realtime", invocation.ingest_source)
        self.assertIsNone(parse_realtime_event(dict(event, toolName="write")))
        self.assertIsNone(parse_realtime_event(dict(event, args={"path": "README.md"})))


class PiExtensionTests(unittest.TestCase):
    def test_source_uses_nonblocking_successful_read_contract(self):
        source = EXTENSION.read_text(encoding="utf-8")
        self.assertIn('pi.on("tool_call"', source)
        self.assertIn('pi.on("tool_execution_end"', source)
        self.assertIn('event.toolName !== "read"', source)
        self.assertIn("event.isError", source)
        self.assertIn("toolCallId", source)
        self.assertNotIn("event.args", source)
        self.assertIn("readPaths.get(event.toolCallId)", source)
        self.assertIn('"record", "--platform", "pi"', source)
        self.assertIn("shell: false", source)
        self.assertIn("detached: true", source)
        self.assertIn('stdio: ["pipe", "ignore", "ignore"]', source)
        self.assertIn("child.unref()", source)
        self.assertNotIn("await child", source)

    @unittest.skipUnless(os.name == "nt" or Path("/usr/bin/env").exists(), "requires Node")
    def test_extension_sends_only_successful_skill_read_event_to_stdin(self):
        node = "node.exe" if os.name == "nt" else "node"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analytics = root / "analytics.py"
            output = root / "event.json"
            skill = root / "skills" / "alpha" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("# alpha", encoding="utf-8")
            analytics.write_text(
                "import pathlib, sys\n"
                f"pathlib.Path({str(output)!r}).write_text(sys.stdin.read(), encoding='utf-8')\n",
                encoding="utf-8",
            )
            source = EXTENSION.read_text(encoding="utf-8")
            source = source.replace("__PYTHON_EXECUTABLE__", json.dumps(sys.executable))
            source = source.replace("__PROJECT_ROOT__", json.dumps(str(root)))
            source = source.replace("__DATABASE_PATH__", json.dumps(str(root / "events.sqlite")))
            module = root / "extension.mjs"
            module.write_text(source, encoding="utf-8")
            missing_cache_runner = (
                f"import({json.dumps(module.as_uri())}).then(async m => {{"
                "const hooks={}; const pi={on:(name,handler)=>hooks[name]=handler};"
                "m.default(pi);"
                f"const ctx={{cwd:{json.dumps(str(root))},sessionManager:{{getSessionId:()=>\"pi-live\"}},"
                "model:{provider:'provider-live',id:'model-live'}};"
                "await hooks.tool_execution_end({toolName:'read',toolCallId:'missing',isError:false,"
                "args:{path:'skills/alpha/SKILL.md'}},ctx);"
                "})"
            )
            missing_cache = subprocess.run(
                [node, "--input-type=module", "-e", missing_cache_runner],
                capture_output=True,
                text=True,
                timeout=5,
                shell=False,
            )
            self.assertEqual(0, missing_cache.returncode, missing_cache.stderr)
            time.sleep(0.1)
            self.assertFalse(output.exists())

            runner = (
                f"import({json.dumps(module.as_uri())}).then(async m => {{"
                "const hooks={}; const pi={on:(name,handler)=>hooks[name]=handler};"
                "m.default(pi);"
                f"const ctx={{cwd:{json.dumps(str(root))},sessionManager:{{getSessionId:()=>\"pi-live\"}},"
                "model:{provider:'provider-live',id:'model-live'}};"
                "await hooks.tool_call({toolName:'read',toolCallId:'failed',input:{path:'skills/alpha/SKILL.md'}},ctx);"
                "await hooks.tool_execution_end({toolName:'read',toolCallId:'failed',isError:true,result:{secret:'x'}},ctx);"
                "await hooks.tool_call({toolName:'read',toolCallId:'other',input:{path:'README.md'}},ctx);"
                "await hooks.tool_execution_end({toolName:'read',toolCallId:'other',isError:false,result:{secret:'x'}},ctx);"
                "await hooks.tool_call({toolName:'read',toolCallId:'ok',input:{path:'skills/alpha/SKILL.md'}},ctx);"
                "await hooks.tool_execution_end({toolName:'read',toolCallId:'ok',isError:false,result:{secret:'x'}},ctx);"
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
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("pi-live", payload["sessionId"])
            self.assertEqual("ok", payload["toolCallId"])
            self.assertEqual("read", payload["toolName"])
            self.assertEqual(str(skill.resolve()), payload["args"]["path"])
            self.assertNotIn("result", payload)


if __name__ == "__main__":
    unittest.main()
