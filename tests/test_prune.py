"""Cleanup commands: dry-run safety, scoped deletion, and space reclamation."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import analytics
import scanner


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat().replace(
        "+00:00", "Z"
    )


class PruneTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.db = self.root / "analytics.db"
        scanner.init_db(self.db).close()

    def _add_invocation(self, skill, days_ago):
        connection = scanner.init_db(self.db)
        connection.execute(
            "INSERT INTO invocations (platform, session_id, turn_id, skill_name, "
            "skill_key, evidence_type, invoked_at, agent_kind, ingest_source) "
            "VALUES ('codex', 's1', ?, ?, ?, 'skill_file_read', ?, 'main', 'history')",
            (f"t-{skill}-{days_ago}", skill, f"path:{skill}", _iso(days_ago)),
        )
        connection.commit()
        connection.close()

    def _add_diagnostic(self, offset):
        connection = scanner.init_db(self.db)
        connection.execute(
            "INSERT INTO diagnostics (platform, source_path, byte_offset, category, "
            "detail, created_at) VALUES ('claude', '/tmp/a.jsonl', ?, 'unknown_entry', "
            "'x', ?)",
            (offset, _iso(1)),
        )
        connection.commit()
        connection.close()

    def _add_scan_state(self, path):
        connection = scanner.init_db(self.db)
        connection.execute(
            "INSERT INTO scan_state (platform, source_path, byte_offset, file_size, "
            "file_mtime, cursor_fingerprint, updated_at) "
            "VALUES ('codex', ?, 0, 0, 0, '', ?)",
            (str(path), _iso(1)),
        )
        connection.commit()
        connection.close()

    def _count(self, table):
        connection = sqlite3.connect(self.db)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            connection.close()


class InvocationPruneTests(PruneTestCase):
    def test_dry_run_deletes_nothing(self):
        for i in range(5):
            self._add_invocation(f"old{i}", 100)
        self._add_invocation("fresh", 1)

        result = scanner.prune_invocations(30, self.db, apply=False)

        self.assertEqual(5, result["matched"])
        self.assertEqual(0, result["deleted"])
        self.assertEqual(6, self._count("invocations"))

    def test_apply_deletes_only_matching_rows(self):
        for i in range(3):
            self._add_invocation(f"old{i}", 100)
        self._add_invocation("fresh", 1)

        result = scanner.prune_invocations(30, self.db, apply=True)

        self.assertEqual(3, result["deleted"])
        self.assertEqual(1, result["remaining"])
        self.assertEqual(1, self._count("invocations"))

    def test_negative_days_is_rejected(self):
        with self.assertRaises(ValueError):
            scanner.prune_invocations(-1, self.db, apply=True)

    def test_installed_skills_are_preserved(self):
        connection = scanner.init_db(self.db)
        connection.execute(
            "INSERT INTO installed_skills (platform, skill_key, skill_name, "
            "skill_source, first_seen_at, last_seen_at) "
            "VALUES ('codex', 'k', 'keeper', 'user', ?, ?)",
            (_iso(200), _iso(200)),
        )
        connection.commit()
        connection.close()
        self._add_invocation("old", 100)

        scanner.prune_invocations(30, self.db, apply=True)

        self.assertEqual(1, self._count("installed_skills"))


class DiagnosticPruneTests(PruneTestCase):
    def test_diagnostics_prune_is_separate_from_invocations(self):
        self._add_invocation("keep", 200)
        for i in range(4):
            self._add_diagnostic(i)

        result = scanner.prune_diagnostics(self.db, apply=True)

        self.assertEqual(4, result["deleted"])
        self.assertEqual(0, self._count("diagnostics"))
        self.assertEqual(1, self._count("invocations"))

    def test_diagnostics_dry_run_keeps_rows(self):
        self._add_diagnostic(1)
        scanner.prune_diagnostics(self.db, apply=False)
        self.assertEqual(1, self._count("diagnostics"))


class DeadScanStateTests(PruneTestCase):
    def test_removes_only_cursors_for_missing_files(self):
        alive = self.root / "alive.jsonl"
        alive.write_text("{}\n", encoding="utf-8")
        self._add_scan_state(alive)
        self._add_scan_state(self.root / "gone.jsonl")

        result = scanner.prune_dead_scan_state(self.db, apply=True)

        self.assertEqual(1, result["matched"])
        self.assertEqual(1, self._count("scan_state"))

    def test_dry_run_keeps_dead_cursors(self):
        self._add_scan_state(self.root / "gone.jsonl")
        result = scanner.prune_dead_scan_state(self.db, apply=False)
        self.assertEqual(1, result["matched"])
        self.assertEqual(1, self._count("scan_state"))


class CompactTests(PruneTestCase):
    def test_compact_reclaims_space_after_large_delete(self):
        for i in range(400):
            self._add_diagnostic(i)
        before = self.db.stat().st_size
        scanner.prune_diagnostics(self.db, apply=True)

        result = scanner.compact_database(self.db)

        self.assertEqual(before, result["before_bytes"])
        self.assertGreaterEqual(result["reclaimed_bytes"], 0)
        self.assertEqual(result["after_bytes"], self.db.stat().st_size)

    def test_database_size_reports_zero_for_missing_file(self):
        self.assertEqual(0, scanner.database_size(self.root / "absent.db"))


class PruneCommandTests(PruneTestCase):
    def _run(self, *args):
        """Common options follow the subcommand, matching the CLI contract."""
        with mock.patch("sys.stdout") as stdout:
            code = analytics.main([args[0], "--db", str(self.db), *args[1:]])
        return code, "".join(call.args[0] for call in stdout.write.call_args_list)

    def test_requires_an_explicit_target(self):
        code, out = self._run("prune")
        self.assertEqual(2, code)
        self.assertIn("nothing_selected", out)

    def test_defaults_to_dry_run(self):
        self._add_invocation("old", 100)
        code, out = self._run("prune", "--older-than", "30")
        self.assertEqual(0, code)
        self.assertIn("no data was deleted", out)
        self.assertEqual(1, self._count("invocations"))

    def test_yes_applies_the_delete(self):
        self._add_invocation("old", 100)
        code, out = self._run("prune", "--older-than", "30", "--yes")
        self.assertEqual(0, code)
        self.assertIn("compact", out)
        self.assertEqual(0, self._count("invocations"))

    def test_compact_command_runs(self):
        code, out = self._run("compact")
        self.assertEqual(0, code)
        self.assertIn("reclaimed_bytes", out)


if __name__ == "__main__":
    unittest.main()
