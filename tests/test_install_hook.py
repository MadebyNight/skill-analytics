import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import install_hook


class InstallHookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.codex_home = Path(self.temp_dir.name) / "codex home"
        self.codex_home.mkdir()
        self.config_path = self.codex_home / "hooks.json"
        self.executable = Path(self.temp_dir.name) / "Python Runtime" / "python.exe"
        self.script = Path(self.temp_dir.name) / "Analytics Tool" / "hook.py"

    def _definition(self):
        return install_hook.hook_definition(self.executable, self.script)

    def test_install_preserves_existing_hooks_and_creates_one_backup(self):
        original = {
            "custom": {"enabled": True},
            "hooks": {
                "SessionEnd": [
                    {"hooks": [{"type": "command", "command": "existing-end"}]}
                ],
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "existing-stop",
                                "statusMessage": "Existing",
                            }
                        ],
                    }
                ],
            },
        }
        original_bytes = json.dumps(original, ensure_ascii=False).encode("utf-8")
        self.config_path.write_bytes(original_bytes)

        changed, backup = install_hook.install(
            self.codex_home, executable=self.executable, hook_script=self.script
        )

        self.assertTrue(changed)
        self.assertIsNotNone(backup)
        self.assertEqual(backup.read_bytes(), original_bytes)
        installed = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(installed["custom"], original["custom"])
        self.assertEqual(installed["hooks"]["SessionEnd"], original["hooks"]["SessionEnd"])
        self.assertEqual(installed["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        self.assertEqual(installed["hooks"]["Stop"][1], self._definition())

        installed_bytes = self.config_path.read_bytes()
        backups = list(self.codex_home.glob("hooks.json.backup-*"))
        changed_again, backup_again = install_hook.install(
            self.codex_home, executable=self.executable, hook_script=self.script
        )
        self.assertFalse(changed_again)
        self.assertIsNone(backup_again)
        self.assertEqual(self.config_path.read_bytes(), installed_bytes)
        self.assertEqual(list(self.codex_home.glob("hooks.json.backup-*")), backups)

    def test_uninstall_removes_only_the_exact_definition(self):
        own = self._definition()
        similar = {
            "hooks": [{**own["hooks"][0], "statusMessage": "A different hook"}]
        }
        existing = {
            "hooks": {
                "Stop": [
                    similar,
                    own,
                    {"hooks": [{"type": "command", "command": "keep-me"}]},
                ],
                "SessionEnd": [
                    {"hooks": [{"type": "command", "command": "also-keep"}]}
                ],
            }
        }
        self.config_path.write_text(json.dumps(existing), encoding="utf-8")

        changed = install_hook.uninstall(
            self.codex_home, executable=self.executable, hook_script=self.script
        )

        self.assertTrue(changed)
        remaining = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(remaining["hooks"]["Stop"], [similar, existing["hooks"]["Stop"][2]])
        self.assertEqual(remaining["hooks"]["SessionEnd"], existing["hooks"]["SessionEnd"])
        self.assertFalse(list(self.codex_home.glob("hooks.json.backup-*")))

        before = self.config_path.read_bytes()
        self.assertFalse(
            install_hook.uninstall(
                self.codex_home, executable=self.executable, hook_script=self.script
            )
        )
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_command_quotes_paths_with_spaces(self):
        definition = self._definition()
        handler = definition["hooks"][0]

        self.assertEqual(set(definition), {"hooks"})
        self.assertEqual(
            set(handler), {"type", "command", "statusMessage", "async"}
        )
        self.assertIs(handler["async"], True)
        self.assertIn(f'"{self.executable}"', handler["command"])
        self.assertIn(f'"{self.script}"', handler["command"])

    def test_cli_uses_codex_home_environment(self):
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.codex_home)
        script = Path(install_hook.__file__).resolve()

        first = subprocess.run(
            [sys.executable, str(script), "install"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        installed_bytes = self.config_path.read_bytes()
        second = subprocess.run(
            [sys.executable, str(script), "install"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        removed = subprocess.run(
            [sys.executable, str(script), "uninstall"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual((first.returncode, second.returncode, removed.returncode), (0, 0, 0))
        self.assertIn("Hook already installed.", second.stdout)
        self.assertNotEqual(self.config_path.read_bytes(), installed_bytes)
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8")),
            {"hooks": {"Stop": []}},
        )

    def test_unified_codex_api_reports_status_and_backs_up_uninstall(self):
        self.config_path.write_text(
            json.dumps({"custom": True, "hooks": {"Stop": []}}), encoding="utf-8"
        )

        installed = install_hook.install_platform("codex", codex_home=self.codex_home)
        installed_bytes = self.config_path.read_bytes()
        self.assertEqual("installed", installed["status"])
        self.assertEqual("installed", install_hook.integration_status(
            "codex", codex_home=self.codex_home
        ))

        unchanged = install_hook.install_platform("codex", codex_home=self.codex_home)
        self.assertEqual("already_installed", unchanged["status"])
        self.assertEqual(installed_bytes, self.config_path.read_bytes())

        removed = install_hook.uninstall_platform("codex", codex_home=self.codex_home)
        self.assertEqual("removed", removed["status"])
        self.assertTrue(Path(removed["backup"]).is_file())
        self.assertEqual(installed_bytes, Path(removed["backup"]).read_bytes())
        self.assertEqual("not_installed", install_hook.integration_status(
            "codex", codex_home=self.codex_home
        ))


if __name__ == "__main__":
    unittest.main()
