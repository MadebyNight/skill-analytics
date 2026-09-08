import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scanner
from adapters.codex import CodexAdapter


FIXTURES = Path(__file__).parent / "fixtures"


class ScannerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "analytics.db"
        self.project_root = self.root / "redacted-project"
        self.project_root.mkdir()
        self.alpha = (FIXTURES / "skills" / "alpha" / "SKILL.md").resolve()
        self.beta = (FIXTURES / "skills" / "beta" / "SKILL.md").resolve()

    def fixture_transcript(self, name):
        content = (FIXTURES / name).read_text(encoding="utf-8")
        content = content.replace("__PROJECT_ROOT__", self.project_root.as_posix())
        content = content.replace("__SKILL_ALPHA__", self.alpha.as_posix())
        content = content.replace("__SKILL_BETA__", self.beta.as_posix())
        path = self.root / name
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def rows(self, query, parameters=()):
        connection = sqlite3.connect(self.db_path)
        try:
            connection.row_factory = sqlite3.Row
            return connection.execute(query, parameters).fetchall()
        finally:
            connection.close()

    def test_verified_exec_reads_are_detected_without_false_positives(self):
        transcript = self.fixture_transcript("main_session.jsonl")

        result = scanner.scan_transcript(transcript, db_path=self.db_path)

        self.assertEqual(3, result.inserted)
        invocations = self.rows(
            "SELECT turn_id, skill_path, agent_kind FROM invocations ORDER BY turn_id"
        )
        self.assertEqual(["turn-1", "turn-2", "turn-3"], [row["turn_id"] for row in invocations])
        self.assertEqual(
            [
                os.path.normcase(str(self.alpha)),
                os.path.normcase(str(self.alpha)),
                os.path.normcase(str(self.beta)),
            ],
            [row["skill_path"] for row in invocations],
        )
        self.assertEqual({"main"}, {row["agent_kind"] for row in invocations})
        stored = self.rows("SELECT DISTINCT session_id, model FROM invocations")
        self.assertEqual({"session-main"}, {row["session_id"] for row in stored})
        self.assertEqual({"gpt-test", "gpt-test-updated"}, {row["model"] for row in stored})

    def test_repeated_scans_and_hook_ingest_are_idempotent(self):
        transcript = self.fixture_transcript("main_session.jsonl")
        scanner.scan_transcript(transcript, db_path=self.db_path, ingest_source="history")

        second = scanner.scan_transcript(
            transcript,
            db_path=self.db_path,
            session_id="session-main",
            ingest_source="hook",
        )

        self.assertEqual(0, second.inserted)
        self.assertEqual(3, self.rows("SELECT COUNT(*) AS count FROM invocations")[0]["count"])

    def test_subagent_and_unknown_are_classified_and_unknown_is_diagnosed(self):
        subagent = self.fixture_transcript("subagent_session.jsonl")
        unknown = self.fixture_transcript("unknown_session.jsonl")

        scanner.scan_transcript(subagent, db_path=self.db_path)
        scanner.scan_transcript(unknown, db_path=self.db_path)

        kinds = self.rows("SELECT session_id, agent_kind, model FROM invocations ORDER BY session_id")
        self.assertEqual({"subagent", "unknown"}, {row["agent_kind"] for row in kinds})
        subagent_row = next(row for row in kinds if row["agent_kind"] == "subagent")
        self.assertEqual("session-subagent", subagent_row["session_id"])
        self.assertEqual("gpt-test", subagent_row["model"])
        diagnostics = self.rows(
            "SELECT category FROM diagnostics WHERE category = 'unknown_agent'"
        )
        self.assertEqual(1, len(diagnostics))

    def test_unrecognized_session_source_is_unknown_not_main(self):
        transcript = self.root / "unknown-source.jsonl"
        events = (
            {
                "timestamp": "2026-09-07T03:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "unknown-source-session",
                    "cwd": str(self.project_root),
                    "source": {"unrecognized": "shape"},
                },
            },
            self._event("ignored-parent", "turn-1", f"cat '{self.alpha}'"),
        )
        transcript.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        scanner.scan_transcript(transcript, db_path=self.db_path)

        row = self.rows("SELECT session_id, agent_kind FROM invocations")[0]
        self.assertEqual("unknown-source-session", row["session_id"])
        self.assertEqual("unknown", row["agent_kind"])
        self.assertEqual(
            1,
            self.rows(
                "SELECT COUNT(*) AS count FROM diagnostics WHERE category = 'unknown_agent'"
            )[0]["count"],
        )

    def test_complete_bad_json_is_skipped_and_does_not_block_later_lines(self):
        transcript = self.root / "damaged.jsonl"
        valid = self._event("damaged-session", "turn-1", f"cat '{self.alpha}'")
        transcript.write_text("{not json}\n" + json.dumps(valid) + "\n", encoding="utf-8")

        result = scanner.scan_transcript(
            transcript,
            db_path=self.db_path,
            session_id="damaged-session",
            cwd=self.project_root,
        )

        self.assertEqual(1, result.inserted)
        self.assertEqual(1, result.parse_errors)
        diagnostic = self.rows("SELECT category, detail FROM diagnostics")[0]
        self.assertEqual("invalid_json", diagnostic["category"])
        self.assertNotIn("not json", diagnostic["detail"])

    def test_unterminated_tail_is_retained_until_next_scan(self):
        transcript = self.root / "growing.jsonl"
        first = json.dumps(self._event("growing-session", "turn-1", f"cat '{self.alpha}'"))
        second = json.dumps(self._event("growing-session", "turn-2", f"cat '{self.beta}'"))
        split = len(second) // 2
        transcript.write_bytes((first + "\n" + second[:split]).encode())

        initial = scanner.scan_transcript(
            transcript,
            db_path=self.db_path,
            session_id="growing-session",
            cwd=self.project_root,
        )
        connection = sqlite3.connect(self.db_path)
        try:
            offset = connection.execute(
                "SELECT byte_offset FROM scan_state WHERE platform = ? AND source_path = ?",
                ("codex", str(transcript.resolve())),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(len((first + "\n").encode()), offset)
        self.assertEqual(1, initial.inserted)

        with transcript.open("ab") as stream:
            stream.write((second[split:] + "\n").encode())
        appended = scanner.scan_transcript(
            transcript,
            db_path=self.db_path,
            session_id="growing-session",
            cwd=self.project_root,
        )

        self.assertEqual(1, appended.inserted)
        self.assertEqual(2, self.rows("SELECT COUNT(*) AS count FROM invocations")[0]["count"])

    def test_truncated_file_is_rescanned_without_duplicate_invocations(self):
        transcript = self.root / "truncated.jsonl"
        original = self._event("truncated-session", "turn-1", f"cat '{self.alpha}'")
        transcript.write_text(json.dumps(original) + "\n", encoding="utf-8")
        scanner.scan_transcript(
            transcript,
            db_path=self.db_path,
            session_id="truncated-session",
            cwd=self.project_root,
        )

        replacement = self._event("truncated-session", "turn-2", f"cat '{self.beta}'")
        transcript.write_text(json.dumps(replacement) + "\n", encoding="utf-8")
        scanner.scan_transcript(
            transcript,
            db_path=self.db_path,
            session_id="truncated-session",
            cwd=self.project_root,
        )

        turns = self.rows("SELECT turn_id FROM invocations ORDER BY turn_id")
        self.assertEqual(["turn-1", "turn-2"], [row["turn_id"] for row in turns])

    def test_relative_skill_path_is_canonicalized_against_cwd(self):
        skill = self.project_root / "local" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\nname: local-skill\n---\n", encoding="utf-8")
        transcript = self.root / "relative.jsonl"
        event = self._event("relative-session", "turn-1", "Get-Content ./local/SKILL.md")
        transcript.write_text(json.dumps(event) + "\n", encoding="utf-8")

        scanner.scan_transcript(
            transcript,
            db_path=self.db_path,
            session_id="relative-session",
            cwd=self.project_root,
        )

        row = self.rows("SELECT skill_path, skill_name FROM installed_skills")[0]
        self.assertEqual(os.path.normcase(str(skill.resolve())), row["skill_path"])
        self.assertEqual("local-skill", row["skill_name"])

    def test_exec_workdir_overrides_session_cwd_for_relative_path(self):
        actual_workdir = self.root / "actual-workdir"
        skill = actual_workdir / "skill" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: workdir-skill\n---\n", encoding="utf-8")
        transcript = self.root / "workdir.jsonl"
        event = self._event(
            "workdir-session",
            "turn-1",
            "Get-Content ./skill/SKILL.md",
            workdir=actual_workdir,
        )
        transcript.write_text(json.dumps(event) + "\n", encoding="utf-8")

        scanner.scan_transcript(
            transcript,
            db_path=self.db_path,
            session_id="workdir-session",
            cwd=self.project_root,
        )

        row = self.rows("SELECT skill_path FROM invocations")[0]
        self.assertEqual(os.path.normcase(str(skill.resolve())), row["skill_path"])

    def test_directory_named_find_is_not_mistaken_for_enumeration(self):
        skill = self.root / "repo" / "find" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: find-directory-skill\n---\n", encoding="utf-8")
        transcript = self.root / "find-directory.jsonl"
        event = self._event("find-session", "turn-1", f"Get-Content '{skill}'")
        transcript.write_text(json.dumps(event) + "\n", encoding="utf-8")

        result = scanner.scan_transcript(transcript, db_path=self.db_path)

        self.assertEqual(1, result.inserted)
        row = self.rows("SELECT skill_path FROM invocations")[0]
        self.assertEqual(os.path.normcase(str(skill.resolve())), row["skill_path"])

    def test_javascript_text_that_only_looks_like_exec_is_ignored(self):
        command = json.dumps(f"Get-Content '{self.alpha}'")
        fake_call = f"tools.exec_command({{cmd: {command}}})"
        inputs = (
            f"const options = {{cmd: {command}}}; text('not executed');",
            f"// {fake_call};",
            f"const example = {json.dumps(fake_call)}; text(example);",
        )

        for tool_input in inputs:
            with self.subTest(tool_input=tool_input):
                payload = {"type": "custom_tool_call", "name": "exec", "input": tool_input}
                self.assertEqual(set(), scanner.extract_skill_paths(payload, self.project_root))

    def test_powershell_output_assignment_and_simple_path_variables(self):
        transcript = self.root / "powershell-variables.jsonl"
        commands = (
            f"$lines=Get-Content -LiteralPath '{self.alpha}' -Raw",
            f"$p='{self.alpha}'; Get-Content $p",
            f'$p="{self.alpha}"; $x=Get-Content -LiteralPath $p',
            "$p = Join-Path $env:CODEX_HOME 'SKILL.md'; Get-Content $p",
            "Get-Content $undefined",
        )
        events = [
            self._event("powershell-session", f"turn-{index}", command)
            for index, command in enumerate(commands, start=1)
        ]
        transcript.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        result = scanner.scan_transcript(transcript, db_path=self.db_path)

        self.assertEqual(3, result.inserted)
        turns = self.rows("SELECT turn_id FROM invocations ORDER BY turn_id")
        self.assertEqual(["turn-1", "turn-2", "turn-3"], [row["turn_id"] for row in turns])

    def test_only_exact_skill_file_operands_are_counted(self):
        transcript = self.root / "strict-operands.jsonl"
        commands = (
            "sed -n '/SKILL.md/p' transcript.jsonl",
            "cat notes-SKILL.md.bak",
            "cat /tmp/SKILL.md.example",
            "head --label=SKILL.md notes.txt",
            f"sed -n '1,120p' '{self.alpha}'",
        )
        events = [
            self._event("strict-session", f"turn-{index}", command)
            for index, command in enumerate(commands, start=1)
        ]
        transcript.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

        result = scanner.scan_transcript(transcript, db_path=self.db_path)

        self.assertEqual(1, result.inserted)
        row = self.rows("SELECT turn_id, skill_path FROM invocations")[0]
        self.assertEqual("turn-5", row["turn_id"])
        self.assertEqual(os.path.normcase(str(self.alpha)), row["skill_path"])

    def test_rewritten_file_larger_than_previous_size_is_rescanned(self):
        transcript = self.root / "rewritten-larger.jsonl"
        first = json.dumps(self._event("rewrite-session", "turn-1", f"cat '{self.alpha}'")) + "\n"
        transcript.write_text(first, encoding="utf-8")
        scanner.scan_transcript(transcript, db_path=self.db_path)

        replacement = json.dumps(
            self._event("rewrite-session", "turn-2", f"cat '{self.beta}'")
        )
        replacement += " " * (len(first.encode("utf-8")) + 100) + "\n"
        transcript.write_text(replacement, encoding="utf-8")

        result = scanner.scan_transcript(transcript, db_path=self.db_path)

        self.assertEqual(1, result.inserted)
        turns = self.rows("SELECT turn_id FROM invocations ORDER BY turn_id")
        self.assertEqual(["turn-1", "turn-2"], [row["turn_id"] for row in turns])

    def test_full_prefix_fingerprint_detects_early_rewrite_with_same_tail(self):
        transcript = self.root / "early-rewrite.jsonl"
        first_event = self._event("fingerprint-session", "turn-1", f"cat '{self.alpha}'")
        filler = {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": "x" * 1000},
        }
        first = json.dumps(first_event) + "\n" + json.dumps(filler) + "\n"
        transcript.write_text(first, encoding="utf-8")
        scanner.scan_transcript(transcript, db_path=self.db_path)

        replacement_event = self._event(
            "fingerprint-session", "turn-2", f"cat '{self.beta}'"
        )
        replacement = json.dumps(replacement_event) + "\n" + json.dumps(filler) + "\n"
        replacement += json.dumps({"type": "event_msg", "payload": {"type": "notice"}}) + "\n"
        self.assertGreater(len(replacement.encode("utf-8")), len(first.encode("utf-8")))
        transcript.write_text(replacement, encoding="utf-8")

        result = scanner.scan_transcript(transcript, db_path=self.db_path)

        self.assertEqual(1, result.inserted)
        turns = self.rows("SELECT turn_id FROM invocations ORDER BY turn_id")
        self.assertEqual(["turn-1", "turn-2"], [row["turn_id"] for row in turns])

    def test_missing_turn_metadata_deduplicates_adjacent_reads_by_turn_boundary(self):
        transcript = self.root / "fallback-turns.jsonl"
        read = self._event("fallback-session", "unused", f"cat '{self.alpha}'")
        read["payload"].pop("internal_chat_message_metadata_passthrough")
        repeated = json.loads(json.dumps(read))
        user_boundary = {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "next"},
        }
        next_turn = json.loads(json.dumps(read))
        transcript.write_text(
            "".join(
                json.dumps(event) + "\n"
                for event in (read, repeated, user_boundary, next_turn)
            ),
            encoding="utf-8",
        )

        result = scanner.scan_transcript(transcript, db_path=self.db_path)

        self.assertEqual(2, result.inserted)
        self.assertEqual(1, result.duplicates)
        turns = self.rows("SELECT turn_id FROM invocations ORDER BY turn_id")
        self.assertEqual(
            ["fallback-turn-0", "fallback-turn-1"],
            [row["turn_id"] for row in turns],
        )

    def test_recent_turn_context_id_is_used_when_tool_metadata_is_missing(self):
        transcript = self.root / "turn-context.jsonl"
        turn_context = {
            "type": "turn_context",
            "payload": {"turn_id": "context-turn-7", "cwd": str(self.project_root)},
        }
        read = self._event("context-session", "unused", f"cat '{self.alpha}'")
        read["payload"].pop("internal_chat_message_metadata_passthrough")
        transcript.write_text(
            "".join(json.dumps(event) + "\n" for event in (turn_context, read, read)),
            encoding="utf-8",
        )

        result = scanner.scan_transcript(transcript, db_path=self.db_path)

        self.assertEqual(1, result.inserted)
        self.assertEqual(1, result.duplicates)
        row = self.rows("SELECT turn_id FROM invocations")[0]
        self.assertEqual("context-turn-7", row["turn_id"])

    def test_backfill_discovers_nested_sessions_and_remains_idempotent(self):
        codex_home = self.root / "codex-home"
        transcript = codex_home / "sessions" / "2026" / "09" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        event = self._event("backfill-session", "turn-1", f"cat '{self.alpha}'")
        transcript.write_text(json.dumps(event) + "\n", encoding="utf-8")

        first = scanner.backfill(db_path=self.db_path, codex_home=codex_home)
        second = scanner.backfill(db_path=self.db_path, codex_home=codex_home)

        self.assertEqual(1, first.files)
        self.assertEqual(1, first.inserted)
        self.assertEqual(0, second.inserted)
        self.assertEqual(1, self.rows("SELECT COUNT(*) AS count FROM invocations")[0]["count"])

    def test_codex_adapter_uses_environment_root_and_only_current_plugin_version(self):
        codex_home = self.root / "codex-home"
        agents_home = self.root / "agents-home"
        transcript = codex_home / "sessions" / "nested" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("", encoding="utf-8")
        self._write_skill(codex_home / "skills" / "alpha" / "SKILL.md", "Alpha")
        self._write_skill(agents_home / "skills" / "beta" / "SKILL.md", "Beta")
        plugin = codex_home / "plugins" / "cache" / "market" / "plugin"
        self._write_skill(plugin / "1.0" / "skills" / "old" / "SKILL.md", "Old")
        self._write_skill(plugin / "2.0" / "skills" / "current" / "SKILL.md", "Current")

        with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            adapter = CodexAdapter(agents_home=agents_home, db_path=self.db_path)

        self.assertEqual(codex_home.resolve(), adapter.resolved_root)
        self.assertEqual([transcript], adapter.discover_transcripts())
        self.assertEqual(
            {"Alpha", "Beta", "Current"},
            {skill.skill_name for skill in adapter.discover_installed_skills()},
        )

    def test_codex_adapter_keeps_partial_status_after_failure_then_success(self):
        codex_home = self.root / "codex-home"
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        exploding = sessions / "a-exploding.jsonl"
        damaged = sessions / "b-damaged.jsonl"
        successful = sessions / "c-successful.jsonl"
        exploding.write_text("ignored\n", encoding="utf-8")
        damaged.write_text("{broken}\n", encoding="utf-8")
        successful.write_text(
            json.dumps(self._event("successful", "turn-1", f"cat '{self.alpha}'")) + "\n",
            encoding="utf-8",
        )
        original_scan = scanner.scan_transcript

        def fail_one(path, *args, **kwargs):
            if Path(path) == exploding:
                raise OSError("injected file failure")
            return original_scan(path, *args, **kwargs)

        adapter = CodexAdapter(codex_home=codex_home, db_path=self.db_path)
        with mock.patch.object(scanner, "scan_transcript", side_effect=fail_one):
            result = adapter.scan()

        self.assertEqual(1, result.failures)
        self.assertEqual(1, result.parse_errors)
        status = self.rows(
            "SELECT status FROM platform_status WHERE platform = 'codex'"
        )[0]["status"]
        self.assertEqual("partial", status)

    @staticmethod
    def _write_skill(path, name):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

    @staticmethod
    def _event(session_id, turn_id, command, workdir=None):
        properties = [f"cmd: {json.dumps(command)}"]
        if workdir is not None:
            properties.append(f"workdir: {json.dumps(str(workdir))}")
        exec_input = "{" + ", ".join(properties) + "}"
        return {
            "timestamp": "2026-09-07T04:00:00Z",
            "session_id": session_id,
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": f"const r = await tools.exec_command({exec_input}); text(r.output);",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            },
        }


if __name__ == "__main__":
    unittest.main()
