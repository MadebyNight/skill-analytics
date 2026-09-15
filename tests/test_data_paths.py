"""Default data location and legacy-data migration behavior."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scanner


class UserDataDirTests(unittest.TestCase):
    def test_honors_explicit_override(self):
        with mock.patch.dict(os.environ, {"SKILL_ANALYTICS_HOME": "/tmp/custom-home"}):
            self.assertEqual(Path("/tmp/custom-home"), scanner._user_data_dir())

    def test_windows_default_uses_localappdata(self):
        env = {"LOCALAPPDATA": r"C:\\Users\\ada\\AppData\\Local"}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict(
            os.environ, {"SKILL_ANALYTICS_HOME": ""}, clear=False
        ):
            self.assertEqual(
                Path(r"C:\\Users\\ada\\AppData\\Local") / "skill-analytics",
                scanner._user_data_dir("nt"),
            )

    def test_posix_default_uses_xdg_data_home(self):
        env = {"XDG_DATA_HOME": "/home/ada/.local/share"}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.dict(
            os.environ, {"SKILL_ANALYTICS_HOME": ""}, clear=False
        ):
            self.assertEqual(
                Path("/home/ada/.local/share/skill-analytics"),
                scanner._user_data_dir("posix"),
            )

    def test_default_location_is_not_inside_repository(self):
        self.assertNotEqual(scanner.PROJECT_ROOT, scanner.DATA_DIR)
        self.assertNotIn(scanner.PROJECT_ROOT, scanner.DATA_DIR.parents)


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.legacy = self.root / "legacy" / "analytics.db"
        self.target = self.root / "user" / "analytics.db"

    def _seed_legacy(self):
        self.legacy.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.legacy)
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('keep')")
        connection.commit()
        connection.close()

    def test_copies_legacy_database_once(self):
        self._seed_legacy()
        with mock.patch.object(scanner, "DEFAULT_DB_PATH", self.target):
            self.assertTrue(scanner.migrate_legacy_data(self.legacy))
            self.assertTrue(self.target.exists())
            self.assertFalse(scanner.migrate_legacy_data(self.legacy))

    def test_never_overwrites_existing_user_data(self):
        self._seed_legacy()
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text("existing", encoding="utf-8")
        with mock.patch.object(scanner, "DEFAULT_DB_PATH", self.target):
            self.assertFalse(scanner.migrate_legacy_data(self.legacy))
        self.assertEqual("existing", self.target.read_text(encoding="utf-8"))

    def test_leaves_legacy_file_in_place(self):
        self._seed_legacy()
        with mock.patch.object(scanner, "DEFAULT_DB_PATH", self.target):
            scanner.migrate_legacy_data(self.legacy)
        self.assertTrue(self.legacy.exists())

    def test_missing_legacy_is_not_an_error(self):
        with mock.patch.object(scanner, "DEFAULT_DB_PATH", self.target):
            self.assertFalse(scanner.migrate_legacy_data(self.root / "absent.db"))

    def test_no_op_when_legacy_is_the_target(self):
        with mock.patch.object(scanner, "DEFAULT_DB_PATH", self.legacy):
            self.assertFalse(scanner.migrate_legacy_data(self.legacy))

    def test_init_db_triggers_migration_for_default_path(self):
        self._seed_legacy()
        with mock.patch.object(scanner, "DEFAULT_DB_PATH", self.target):
            connection = scanner.init_db(self.target)
            connection.close()

            legacy = sqlite3.connect(self.legacy)
            try:
                rows = [row[0] for row in legacy.execute("SELECT value FROM marker")]
            finally:
                legacy.close()

            self.assertEqual(["keep"], rows)
            self.assertTrue(self.target.exists())


if __name__ == "__main__":
    unittest.main()
