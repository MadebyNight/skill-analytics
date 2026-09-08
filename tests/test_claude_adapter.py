import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scanner
from adapters.base import canonical_path
from adapters.claude import (
    ClaudeAdapter,
    parse_post_tool_use,
    parse_user_prompt_expansion,
)


FIXTURES = Path(__file__).parent / "fixtures" / "claude"


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.claude_home = self.root / "claude-home"
        self.project = self.root / "workspace" / "project"
        self.project.mkdir(parents=True)
        self.db_path = self.root / "analytics.db"
        self.alpha = self.claude_home / "skills" / "alpha" / "SKILL.md"
        self.beta = self.project / ".claude" / "skills" / "beta" / "SKILL.md"
        self._write_skill(self.alpha, "Alpha")
        self._write_skill(self.beta, "Beta")

    @staticmethod
    def _write_skill(path, name):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

    def _copy_fixture(self, fixture, destination):
        content = (FIXTURES / fixture).read_text(encoding="utf-8")
        content = content.replace("__PROJECT_ROOT__", self.project.as_posix())
        content = content.replace("__SKILL_ALPHA__", self.alpha.as_posix())
        content = content.replace("__SKILL_BETA__", self.beta.as_posix())
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8", newline="\n")
        return destination

    def _rows(self, query):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.row_factory = sqlite3.Row
            return connection.execute(query).fetchall()
        finally:
            connection.close()

    def test_discovers_config_projects_subagents_and_skill_roots(self):
        main = self._copy_fixture(
            "main.jsonl", self.claude_home / "projects" / "redacted" / "session-main.jsonl"
        )
        subagent = self._copy_fixture(
            "subagent.jsonl",
            self.claude_home
            / "projects"
            / "redacted"
            / "session-main"
            / "subagents"
            / "agent-redacted.jsonl",
        )
        ancestor_skill = self.project.parent / ".claude" / "skills" / "ancestor" / "SKILL.md"
        plugin_skill = (
            self.claude_home
            / "plugins"
            / "cache"
            / "market"
            / "plugin"
            / "1.0"
            / "skills"
            / "plugin-skill"
            / "SKILL.md"
        )
        self._write_skill(ancestor_skill, "Ancestor")
        self._write_skill(plugin_skill, "Plugin Skill")

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude_home)}):
            adapter = ClaudeAdapter(project_dirs=[self.project], db_path=self.db_path)

        self.assertEqual(self.claude_home.resolve(), adapter.resolved_root)
        self.assertEqual([main, subagent], adapter.discover_transcripts())
        discovered = adapter.discover_installed_skills()
        self.assertEqual(
            {"Alpha", "Beta", "Ancestor", "Plugin Skill"},
            {skill.skill_name for skill in discovered},
        )
        self.assertEqual(
            "plugin",
            next(skill.skill_source for skill in discovered if skill.skill_name == "Plugin Skill"),
        )

    def test_scan_parses_tested_entries_deduplicates_evidence_and_marks_partial(self):
        main = self._copy_fixture(
            "main.jsonl", self.claude_home / "projects" / "redacted" / "session-main.jsonl"
        )
        self._copy_fixture(
            "subagent.jsonl",
            main.parent / "session-main" / "subagents" / "agent-redacted.jsonl",
        )
        adapter = ClaudeAdapter(
            claude_home=self.claude_home,
            project_dirs=[self.project],
            db_path=self.db_path,
        )

        result = adapter.scan()

        self.assertEqual(4, result.inserted)
        self.assertGreaterEqual(result.parse_errors, 2)
        rows = self._rows(
            "SELECT session_id, turn_id, skill_name, skill_path, evidence_type, "
            "agent_kind, model, cwd, invoked_at FROM invocations "
            "WHERE platform = 'claude' ORDER BY invoked_at"
        )
        self.assertEqual(
            [
                ("toolu-skill", "Alpha", "structured_skill", "main", "claude-sonnet-test"),
                ("turn-2", "Beta", "skill_file_read", "main", "claude-sonnet-test"),
                ("turn-3", "Alpha", "slash_skill", "main", None),
                ("toolu-sub-skill", "Beta", "structured_skill", "subagent", "claude-haiku-test"),
            ],
            [
                (row["turn_id"], row["skill_name"], row["evidence_type"], row["agent_kind"], row["model"])
                for row in rows
            ],
        )
        self.assertEqual(canonical_path(self.alpha), rows[0]["skill_path"])
        self.assertEqual(canonical_path(self.project), canonical_path(rows[0]["cwd"]))
        self.assertEqual("2026-09-08T01:00:00Z", rows[0]["invoked_at"])
        categories = {row["category"] for row in self._rows("SELECT category FROM diagnostics")}
        self.assertIn("unsupported_skill_input", categories)
        self.assertIn("unknown_entry", categories)
        status = self._rows("SELECT status FROM platform_status WHERE platform = 'claude'")[0]
        self.assertEqual("partial", status["status"])

        repeated = adapter.scan()
        self.assertEqual(0, repeated.inserted)
        self.assertEqual(0, repeated.lines)

    def test_scan_resumes_appends_and_restarts_after_truncation(self):
        transcript = self.claude_home / "projects" / "redacted" / "append.jsonl"
        transcript.parent.mkdir(parents=True)
        first = {
            "type": "assistant", "sessionId": "append", "uuid": "one",
            "timestamp": "2026-09-08T02:00:00Z",
            "message": {"role": "assistant", "model": "claude-test", "content": [
                {"type": "tool_use", "id": "tool-1", "name": "Skill", "input": {"skill": "alpha"}}
            ]},
        }
        second = json.loads(json.dumps(first))
        second.update(uuid="two", timestamp="2026-09-08T02:01:00Z")
        second["message"]["content"][0]["id"] = "tool-2"
        transcript.write_text(json.dumps(first) + "\n", encoding="utf-8")
        adapter = ClaudeAdapter(claude_home=self.claude_home, db_path=self.db_path)
        self.assertEqual(1, adapter.scan().inserted)

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(second) + "\n")
        self.assertEqual(1, adapter.scan().inserted)

        transcript.write_text(json.dumps(second) + "\n", encoding="utf-8")
        rescanned = adapter.scan()
        self.assertEqual(0, rescanned.inserted)
        self.assertEqual(1, rescanned.duplicates)
        self.assertEqual(2, len(self._rows("SELECT turn_id FROM invocations")))

    def test_history_and_realtime_skill_events_deduplicate_in_both_orders(self):
        transcript = self.claude_home / "projects" / "redacted" / "hook-session.jsonl"
        transcript.parent.mkdir(parents=True)
        entry = {
            "type": "assistant",
            "sessionId": "hook-session",
            "uuid": "message-1",
            "cwd": str(self.project),
            "timestamp": "2026-09-08T02:00:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-test",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-shared",
                        "name": "Skill",
                        "input": {"skill": "alpha"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu-read-fallback",
                        "name": "Read",
                        "input": {"file_path": str(self.alpha)},
                    },
                ],
            },
        }
        transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "hook-session",
            "tool_use_id": "toolu-shared",
            "tool_name": "Skill",
            "tool_input": {"skill": "alpha"},
            "cwd": str(self.project),
        }

        for first_source in ("realtime", "history"):
            with self.subTest(first_source=first_source):
                case_db = self.root / f"{first_source}.db"
                adapter = ClaudeAdapter(
                    claude_home=self.claude_home,
                    project_dirs=[self.project],
                    db_path=case_db,
                )
                realtime = parse_post_tool_use(payload)
                self.assertIsNotNone(realtime)
                if first_source == "realtime":
                    self.assertEqual(1, scanner.store_invocations([realtime], case_db).inserted)
                    scan_result = adapter.scan()
                    self.assertEqual(0, scan_result.inserted)
                    self.assertEqual(1, scan_result.duplicates)
                else:
                    self.assertEqual(1, adapter.scan().inserted)
                    stored = scanner.store_invocations([realtime], case_db)
                    self.assertEqual(0, stored.inserted)
                    self.assertEqual(1, stored.duplicates)

                connection = sqlite3.connect(case_db)
                try:
                    rows = connection.execute(
                        "SELECT turn_id, evidence_type FROM invocations WHERE platform = 'claude'"
                    ).fetchall()
                finally:
                    connection.close()
                self.assertEqual([("toolu-shared", "structured_skill")], rows)

    def test_realtime_structured_evidence_wins_over_history_read_in_both_orders(self):
        transcript = self.claude_home / "projects" / "redacted" / "hook-session.jsonl"
        transcript.parent.mkdir(parents=True)
        entry = {
            "type": "assistant",
            "sessionId": "hook-session",
            "uuid": "toolu-shared",
            "cwd": str(self.project),
            "timestamp": "2026-09-08T02:00:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-test",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-read",
                        "name": "Read",
                        "input": {"file_path": str(self.alpha)},
                    }
                ],
            },
        }
        transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        realtime = parse_post_tool_use({
            "hook_event_name": "PostToolUse",
            "session_id": "hook-session",
            "tool_use_id": "toolu-shared",
            "tool_name": "Skill",
            "tool_input": {"skill": "alpha"},
            "cwd": str(self.project),
        })
        self.assertIsNotNone(realtime)

        for first_source in ("realtime", "history"):
            with self.subTest(first_source=first_source):
                case_db = self.root / f"upgrade-{first_source}.db"
                adapter = ClaudeAdapter(
                    claude_home=self.claude_home,
                    project_dirs=[self.project],
                    db_path=case_db,
                )
                if first_source == "realtime":
                    scanner.store_invocations([realtime], case_db)
                    result = adapter.scan()
                    self.assertEqual(1, result.duplicates)
                else:
                    adapter.scan()
                    result = scanner.store_invocations([realtime], case_db)
                    self.assertEqual(1, result.upgraded)

                connection = sqlite3.connect(case_db)
                try:
                    rows = connection.execute(
                        "SELECT turn_id, evidence_type FROM invocations WHERE platform = 'claude'"
                    ).fetchall()
                finally:
                    connection.close()
                self.assertEqual([("toolu-shared", "structured_skill")], rows)

    def test_uninstalled_adapter_isolated_as_not_installed(self):
        missing = self.root / "missing-claude"
        result = ClaudeAdapter(claude_home=missing, db_path=self.db_path).scan()
        self.assertEqual(scanner.ScanResult(), result)
        row = self._rows("SELECT status FROM platform_status WHERE platform = 'claude'")[0]
        self.assertEqual("not_installed", row["status"])

    def test_realtime_payload_parsers_use_only_verified_fields(self):
        post = json.loads((FIXTURES / "post_tool_use.json").read_text(encoding="utf-8"))
        invocation = parse_post_tool_use(post)
        self.assertEqual("hook-session", invocation.session_id)
        self.assertEqual("toolu-hook-1", invocation.turn_id)
        self.assertEqual("alpha", invocation.skill_name)
        self.assertEqual("structured_skill", invocation.evidence_type)
        self.assertEqual("subagent", invocation.agent_kind)
        self.assertEqual("realtime", invocation.ingest_source)
        self.assertIsNone(invocation.skill_path)

        unknown = dict(post, tool_input={"prompt": "alpha"})
        self.assertIsNone(parse_post_tool_use(unknown))
        self.assertIsNone(parse_post_tool_use(dict(post, tool_name="Read")))

        slash = json.loads(
            (FIXTURES / "user_prompt_expansion.json").read_text(encoding="utf-8")
        )
        slash_invocation = parse_user_prompt_expansion(slash)
        self.assertEqual("prompt-1", slash_invocation.turn_id)
        self.assertEqual("alpha", slash_invocation.skill_name)
        self.assertEqual("slash_skill", slash_invocation.evidence_type)
        self.assertEqual("main", slash_invocation.agent_kind)
        self.assertIsNone(
            parse_user_prompt_expansion(dict(slash, expansion_type="mcp_prompt"))
        )

        official = dict(slash)
        official.pop("prompt_id")
        self.assertIsNone(parse_user_prompt_expansion(official))

        with_event_id = dict(official, event_id="event-1")
        self.assertEqual(
            "event-1", parse_user_prompt_expansion(with_event_id).turn_id
        )

        with_both_ids = dict(with_event_id, prompt_id="prompt-first")
        self.assertEqual(
            "prompt-first", parse_user_prompt_expansion(with_both_ids).turn_id
        )


if __name__ == "__main__":
    unittest.main()
