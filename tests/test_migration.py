import sqlite3
import subprocess
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scanner
from adapters.base import Invocation


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "analytics.db"

    def _create_legacy_database(self, invocation_count=1169):
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE skills (
                skill_path TEXT PRIMARY KEY,
                skill_name TEXT NOT NULL,
                skill_source TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE invocations (
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                skill_path TEXT NOT NULL,
                invoked_at TEXT NOT NULL,
                cwd TEXT,
                agent_kind TEXT NOT NULL,
                model TEXT,
                ingest_source TEXT NOT NULL,
                UNIQUE (session_id, turn_id, skill_path)
            );
            CREATE TABLE scan_state (
                transcript_path TEXT PRIMARY KEY,
                byte_offset INTEGER NOT NULL,
                file_size INTEGER NOT NULL,
                file_mtime INTEGER NOT NULL,
                cursor_fingerprint TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE diagnostics (
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
            """
        )
        connection.execute(
            "INSERT INTO skills VALUES (?, ?, ?, ?, ?)",
            ("C:/skills/alpha/SKILL.md", "Alpha", "agents", "2026-01-01Z", "2026-09-08Z"),
        )
        connection.executemany(
            "INSERT INTO invocations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "session-1",
                    f"turn-{index}",
                    "C:/skills/alpha/SKILL.md",
                    "2026-09-08T00:00:00Z",
                    "C:/project",
                    "main",
                    "gpt-test",
                    "hook" if index == 0 else "history",
                )
                for index in range(invocation_count)
            ),
        )
        connection.execute(
            "INSERT INTO scan_state VALUES (?, ?, ?, ?, ?, ?)",
            ("C:/sessions/one.jsonl", 12, 12, 123, "digest", "2026-09-08Z"),
        )
        connection.execute(
            "INSERT INTO diagnostics "
            "(transcript_path, byte_offset, line_number, event_type, category, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("C:/sessions/one.jsonl", 3, 1, "response_item", "invalid_json", "bad", "2026-09-08Z"),
        )
        connection.commit()
        connection.close()

    def test_legacy_codex_database_migrates_without_changing_1169_call_count(self):
        self._create_legacy_database()
        connection = scanner.init_db(self.db_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
            invocation = connection.execute(
                "SELECT platform, skill_name, skill_key, evidence_type, ingest_source "
                "FROM invocations WHERE turn_id = 'turn-0'"
            ).fetchone()
            installed = connection.execute(
                "SELECT platform, skill_key, skill_name FROM installed_skills"
            ).fetchone()
            state = connection.execute(
                "SELECT platform, source_path FROM scan_state"
            ).fetchone()
            diagnostic = connection.execute(
                "SELECT platform, source_path, adapter_version, format_version FROM diagnostics"
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(1169, count)
        self.assertEqual(
            ("codex", "Alpha", "C:/skills/alpha/SKILL.md", "skill_file_read", "realtime"),
            invocation,
        )
        self.assertEqual(("codex", "C:/skills/alpha/SKILL.md", "Alpha"), installed)
        self.assertEqual(("codex", "C:/sessions/one.jsonl"), state)
        self.assertEqual(("codex", "C:/sessions/one.jsonl", "legacy", "legacy"), diagnostic)

    def test_migration_failure_rolls_back_the_legacy_schema_and_rows(self):
        self._create_legacy_database(invocation_count=2)
        with mock.patch.object(scanner, "_validate_migration", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                scanner.init_db(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(invocations)")]
            count = connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
            installed_exists = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='installed_skills'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotIn("platform", columns)
        self.assertEqual(2, count)
        self.assertEqual(0, installed_exists)

    def test_cross_platform_keys_do_not_conflict_and_stronger_evidence_upgrades(self):
        path = str((Path(self.temp_dir.name) / "skills" / "alpha" / "SKILL.md").resolve())
        common = dict(
            session_id="session-1",
            turn_id="turn-1",
            skill_name="Alpha",
            skill_path=path,
            skill_key=path,
            invoked_at="2026-09-08T00:00:00Z",
            cwd=None,
            agent_kind="main",
            model=None,
            ingest_source="history",
        )
        invocations = [
            Invocation(platform="codex", evidence_type="skill_file_read", **common),
            Invocation(platform="codex", evidence_type="structured_skill", **common),
            Invocation(platform="claude", evidence_type="structured_skill", **common),
        ]
        result = scanner.store_invocations(invocations, db_path=self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT platform, evidence_type FROM invocations ORDER BY platform"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(2, result.inserted)
        self.assertEqual(1, result.upgraded)
        self.assertEqual(
            [("claude", "structured_skill"), ("codex", "structured_skill")], rows
        )

    def test_later_path_evidence_merges_a_name_fallback_in_the_same_turn(self):
        path = str((Path(self.temp_dir.name) / "alpha" / "SKILL.md").resolve())
        common = dict(
            platform="claude", session_id="s", turn_id="t", skill_name="Alpha",
            invoked_at="2026-09-08Z", cwd=None, agent_kind="main", model=None,
            ingest_source="history",
        )
        first = scanner.store_invocations(
            [Invocation(skill_path=None, skill_key="name:alpha", evidence_type="structured_skill", **common)],
            self.db_path,
        )
        second = scanner.store_invocations(
            [Invocation(skill_path=path, skill_key=path, evidence_type="skill_file_read", **common)],
            self.db_path,
        )

        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT skill_key, evidence_type FROM invocations"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(1, first.inserted)
        self.assertEqual(1, second.duplicates)
        self.assertEqual([(os.path.normcase(path), "structured_skill")], rows)

    def test_later_name_evidence_upgrades_an_existing_path_in_the_same_turn(self):
        path = str((Path(self.temp_dir.name) / "alpha" / "SKILL.md").resolve())
        common = dict(
            platform="claude", session_id="s", turn_id="t", skill_name="Alpha",
            invoked_at="2026-09-08Z", cwd=None, agent_kind="main", model=None,
            ingest_source="history",
        )
        scanner.store_invocations(
            [Invocation(skill_path=path, skill_key=path, evidence_type="skill_file_read", **common)],
            self.db_path,
        )

        result = scanner.store_invocations(
            [Invocation(skill_path=None, skill_key="name:alpha", evidence_type="structured_skill", **common)],
            self.db_path,
        )

        connection = sqlite3.connect(self.db_path)
        try:
            rows = connection.execute(
                "SELECT skill_key, evidence_type FROM invocations"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(1, result.upgraded)
        self.assertEqual([(os.path.normcase(path), "structured_skill")], rows)

    def test_existing_name_and_path_keys_merge_using_the_stronger_evidence(self):
        path = os.path.normcase(str((Path(self.temp_dir.name) / "alpha" / "SKILL.md").resolve()))
        connection = scanner.init_db(self.db_path)
        try:
            rows = (
                ("claude", "s", "t", "Alpha", None, "name:alpha", "structured_skill"),
                ("claude", "s", "t", "Alpha", path, path, "skill_file_read"),
            )
            connection.executemany(
                """INSERT INTO invocations
                   (platform, session_id, turn_id, skill_name, skill_path, skill_key,
                    evidence_type, invoked_at, cwd, agent_kind, model, ingest_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '2026-09-08Z', NULL, 'main', NULL, 'history')""",
                rows,
            )
            connection.commit()
        finally:
            connection.close()

        scanner.store_invocations(
            [
                Invocation(
                    "claude", "s", "t", "Alpha", path, path, "skill_file_read",
                    "2026-09-08Z", None, "main", None, "history"
                )
            ],
            self.db_path,
        )

        connection = sqlite3.connect(self.db_path)
        try:
            stored = connection.execute(
                "SELECT skill_key, evidence_type FROM invocations"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([(path, "structured_skill")], stored)

    def test_name_only_invocation_uses_unique_installed_path(self):
        path = str((Path(self.temp_dir.name) / "skills" / "alpha" / "SKILL.md").resolve())
        scanner.store_installed_skills(
            [
                scanner.InstalledSkill(
                    "claude", path, "Alpha", path, "user", "2026-09-08Z", "2026-09-08Z"
                )
            ],
            self.db_path,
        )
        invocation = Invocation(
            "claude", "s", "t", "alpha", None, "name:alpha", "structured_skill",
            "2026-09-08Z", None, "main", None, "realtime"
        )

        scanner.store_invocations([invocation], self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            stored = connection.execute(
                "SELECT skill_key, skill_path FROM invocations"
            ).fetchone()
        finally:
            connection.close()
        normalized = os.path.normcase(path)
        self.assertEqual((normalized, normalized), stored)

    def test_ambiguous_name_uses_fallback_key_and_records_diagnostic(self):
        skills = [
            scanner.InstalledSkill(
                "claude", f"path-{index}", "Alpha", f"path-{index}", "user",
                "2026-09-08Z", "2026-09-08Z"
            )
            for index in range(2)
        ]
        scanner.store_installed_skills(skills, self.db_path)
        invocation = Invocation(
            "claude", "s", "t", "ALPHA", None, "ignored", "structured_skill",
            "2026-09-08Z", None, "main", None, "realtime"
        )

        scanner.store_invocations([invocation], self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            key = connection.execute("SELECT skill_key FROM invocations").fetchone()[0]
            category = connection.execute("SELECT category FROM diagnostics").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual("name:alpha", key)
        self.assertEqual("ambiguous_skill_identity", category)

    def test_platform_status_preserves_independent_history_and_realtime_timestamps(self):
        scanner.update_platform_status(
            "codex", "ready", db_path=self.db_path, resolved_root="C:/codex",
            last_history_scan_at="2026-09-08T01:00:00Z"
        )
        scanner.update_platform_status(
            "codex", "integration_error", db_path=self.db_path,
            last_realtime_at="2026-09-08T02:00:00Z"
        )

        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT status, resolved_root, last_history_scan_at, last_realtime_at "
                "FROM platform_status"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(
            ("integration_error", "C:/codex", "2026-09-08T01:00:00Z", "2026-09-08T02:00:00Z"),
            row,
        )

    def test_inventory_replacement_only_removes_stale_rows_when_complete(self):
        root = Path(self.temp_dir.name)
        current_path = os.path.normcase(str((root / "current" / "SKILL.md").resolve()))
        stale_path = os.path.normcase(str((root / "stale" / "SKILL.md").resolve()))
        skills = [
            scanner.InstalledSkill(
                "codex", path, name, path, "other", "2026-09-08Z", "2026-09-08Z"
            )
            for path, name in ((current_path, "Current"), (stale_path, "Stale"))
        ]
        scanner.replace_installed_skills("codex", skills, self.db_path, complete=True)

        def incomplete_discovery():
            yield skills[0]
            raise OSError("injected discovery failure")

        with self.assertRaisesRegex(OSError, "injected"):
            scanner.replace_installed_skills(
                "codex", incomplete_discovery(), self.db_path, complete=True
            )

        scanner.replace_installed_skills("codex", skills[:1], self.db_path, complete=False)
        connection = sqlite3.connect(self.db_path)
        try:
            partial_count = connection.execute(
                "SELECT COUNT(*) FROM installed_skills WHERE platform = 'codex'"
            ).fetchone()[0]
        finally:
            connection.close()

        scanner.replace_installed_skills("codex", skills[:1], self.db_path, complete=True)
        connection = sqlite3.connect(self.db_path)
        try:
            complete_rows = connection.execute(
                "SELECT skill_name FROM installed_skills WHERE platform = 'codex'"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(2, partial_count)
        self.assertEqual([("Current",)], complete_rows)

    def test_two_processes_store_the_same_event_once(self):
        connection = scanner.init_db(self.db_path)
        connection.close()
        code = """
import sys
from adapters.base import Invocation
import scanner
scanner.store_invocations([Invocation(
    'codex', 'session', 'turn', 'Alpha', None, 'name:alpha',
    'structured_skill', '2026-09-08Z', None, 'main', None, 'realtime'
)], sys.argv[1])
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(self.db_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        failures = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            if process.returncode:
                failures.append((process.returncode, stdout, stderr))
        self.assertEqual([], failures)

        connection = sqlite3.connect(self.db_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(1, count)


if __name__ == "__main__":
    unittest.main()
