import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path, PurePosixPath, PureWindowsPath

import install_hook


class PlatformInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_claude_merges_both_hooks_backs_up_and_is_byte_idempotent(self):
        home = self.root / "Claude Home"
        home.mkdir()
        settings = home / "settings.json"
        original = {
            "theme": "dark",
            "hooks": {
                "PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "keep"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "also-keep"}]}],
            },
        }
        original_bytes = json.dumps(original, ensure_ascii=False).encode("utf-8")
        settings.write_bytes(original_bytes)

        result = install_hook.install_platform("claude", claude_home=home)

        self.assertEqual("installed", result["status"])
        self.assertEqual(original_bytes, Path(result["backup"]).read_bytes())
        value = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual("dark", value["theme"])
        self.assertEqual(original["hooks"]["SessionEnd"], value["hooks"]["SessionEnd"])
        self.assertEqual(original["hooks"]["PostToolUse"][0], value["hooks"]["PostToolUse"][0])
        post = value["hooks"]["PostToolUse"][1]
        slash = value["hooks"]["UserPromptExpansion"][0]
        self.assertEqual("Skill", post["matcher"])
        self.assertEqual("", slash["matcher"])
        for group in (post, slash):
            handler = group["hooks"][0]
            self.assertEqual("command", handler["type"])
            self.assertTrue(handler["async"])
            self.assertTrue(Path(handler["command"]).is_absolute())
            self.assertTrue(Path(handler["args"][0]).is_absolute())
            self.assertEqual(
                [
                    "record", "--platform", "claude", "--db",
                    str(install_hook.DEFAULT_DB_PATH.resolve()),
                ],
                handler["args"][1:],
            )

        installed_bytes = settings.read_bytes()
        backups = list(home.glob("settings.json.backup-*"))
        again = install_hook.install_platform("claude", claude_home=home)
        self.assertEqual("already_installed", again["status"])
        self.assertEqual(installed_bytes, settings.read_bytes())
        self.assertEqual(backups, list(home.glob("settings.json.backup-*")))

    def test_claude_uninstall_removes_only_exact_groups_and_backs_up(self):
        home = self.root / "claude"
        install_hook.install_platform("claude", claude_home=home)
        settings = home / "settings.json"
        value = json.loads(settings.read_text(encoding="utf-8"))
        value["hooks"]["PostToolUse"].insert(
            0, {"matcher": "Skill", "hooks": [{"type": "command", "command": "foreign"}]}
        )
        settings.write_text(json.dumps(value), encoding="utf-8")
        before = settings.read_bytes()

        result = install_hook.uninstall_platform("claude", claude_home=home)

        self.assertEqual("removed", result["status"])
        self.assertEqual(before, Path(result["backup"]).read_bytes())
        remaining = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual("foreign", remaining["hooks"]["PostToolUse"][0]["hooks"][0]["command"])
        self.assertEqual([], remaining["hooks"]["UserPromptExpansion"])
        self.assertEqual("not_installed", install_hook.integration_status("claude", claude_home=home))

    def test_claude_rejects_invalid_existing_hook_shape_without_writing(self):
        home = self.root / "claude-invalid"
        home.mkdir()
        settings = home / "settings.json"
        original = b'{"hooks":{"PostToolUse":"wrong"}}'
        settings.write_bytes(original)

        with self.assertRaises(ValueError):
            install_hook.install_platform("claude", claude_home=home)

        self.assertEqual(original, settings.read_bytes())
        self.assertEqual([], list(home.glob("settings.json.backup-*")))

    def test_opencode_and_pi_render_templates_with_json_path_literals(self):
        cases = (
            ("opencode", {"opencode_config_dir": self.root / "Open Code"}, self.root / "Open Code" / "plugins" / "opencode-skill-analytics.js"),
            ("pi", {"pi_home": self.root / "Pi Agent"}, self.root / "Pi Agent" / "extensions" / "pi-skill-analytics.ts"),
        )
        for platform, kwargs, target in cases:
            with self.subTest(platform=platform):
                result = install_hook.install_platform(platform, **kwargs)
                self.assertEqual("installed", result["status"])
                content = target.read_text(encoding="utf-8")
                self.assertTrue(content.startswith(install_hook.GENERATED_SENTINEL))
                self.assertNotIn("__PYTHON_EXECUTABLE__", content)
                self.assertNotIn("__PROJECT_ROOT__", content)
                python_literal = json.dumps(str(Path(install_hook.sys.executable).resolve()))
                project_literal = json.dumps(str(Path(install_hook.__file__).resolve().parent))
                self.assertIn(python_literal, content)
                self.assertIn(project_literal, content)
                before = target.read_bytes()
                self.assertEqual("already_installed", install_hook.install_platform(platform, **kwargs)["status"])
                self.assertEqual(before, target.read_bytes())
                self.assertEqual("installed", install_hook.integration_status(platform, **kwargs))

    def test_all_platform_handlers_persist_custom_database_and_codex_output(self):
        database = (self.root / "Custom Data" / "events.sqlite").resolve()
        output = (self.root / "Custom Reports" / "dashboard.html").resolve()
        homes = {
            "codex": {"codex_home": self.root / "codex"},
            "claude": {"claude_home": self.root / "claude"},
            "opencode": {"opencode_config_dir": self.root / "opencode"},
            "pi": {"pi_home": self.root / "pi"},
        }

        for platform, kwargs in homes.items():
            with self.subTest(platform=platform):
                result = install_hook.install_platform(
                    platform, db_path=database, output_path=output, **kwargs
                )
                content = Path(result["path"]).read_text(encoding="utf-8")
                if platform == "codex":
                    command = json.loads(content)["hooks"]["Stop"][0]["hooks"][0]["command"]
                    self.assertIn(str(database), command)
                    self.assertIn(str(output), command)
                elif platform == "claude":
                    handler = json.loads(content)["hooks"]["PostToolUse"][0]["hooks"][0]
                    self.assertIn(str(database), handler["args"])
                else:
                    self.assertIn(json.dumps(str(database)), content)
                self.assertEqual(
                    "installed",
                    install_hook.integration_status(
                        platform, db_path=database, output_path=output, **kwargs
                    ),
                )

    def test_uninstall_is_identity_exact_but_removes_legacy_installation(self):
        home = self.root / "claude-identity"
        first_db = self.root / "first.sqlite"
        second_db = self.root / "second.sqlite"
        install_hook.install_platform("claude", claude_home=home, db_path=first_db)

        self.assertEqual(
            "not_installed",
            install_hook.uninstall_platform(
                "claude", claude_home=home, db_path=second_db
            )["status"],
        )
        self.assertEqual(
            "installed",
            install_hook.integration_status(
                "claude", claude_home=home, db_path=first_db
            ),
        )

        legacy_home = self.root / "claude-legacy"
        legacy_settings = legacy_home / "settings.json"
        legacy_settings.parent.mkdir(parents=True)
        legacy_settings.write_text(
            json.dumps({"hooks": {
                event: [definition]
                for event, definition in install_hook.claude_hook_definitions().items()
            }}),
            encoding="utf-8",
        )
        removed = install_hook.uninstall_platform(
            "claude", claude_home=legacy_home, db_path=first_db
        )
        self.assertEqual("removed", removed["status"])

        for platform, kwargs, target in (
            (
                "opencode",
                {"opencode_config_dir": self.root / "opencode-legacy"},
                self.root / "opencode-legacy" / "plugins" / "opencode-skill-analytics.js",
            ),
            (
                "pi",
                {"pi_home": self.root / "pi-legacy"},
                self.root / "pi-legacy" / "extensions" / "pi-skill-analytics.ts",
            ),
        ):
            target.parent.mkdir(parents=True)
            target.write_text(
                install_hook.GENERATED_SENTINEL + "// legacy generated integration\n",
                encoding="utf-8",
            )
            result = install_hook.uninstall_platform(platform, db_path=first_db, **kwargs)
            self.assertEqual("removed", result["status"])
            self.assertFalse(target.exists())

    def test_claude_handler_subprocess_writes_to_custom_database(self):
        home = self.root / "claude-subprocess"
        database = self.root / "subprocess data" / "events.sqlite"
        result = install_hook.install_platform(
            "claude", claude_home=home, db_path=database
        )
        settings = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
        handler = settings["hooks"]["PostToolUse"][0]["hooks"][0]
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Skill",
            "session_id": "subprocess-session",
            "tool_use_id": "subprocess-call",
            "tool_input": {"skill": "subprocess-skill"},
        }

        completed = subprocess.run(
            [handler["command"], *handler["args"]],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(
                "SELECT platform, session_id, turn_id, skill_name FROM invocations"
            ).fetchone()
        self.assertEqual(
            ("claude", "subprocess-session", "subprocess-call", "subprocess-skill"),
            row,
        )

    def test_plugin_conflict_is_never_overwritten_or_deleted(self):
        config = self.root / "opencode"
        target = config / "plugins" / "opencode-skill-analytics.js"
        target.parent.mkdir(parents=True)
        foreign = b"export default function foreign() {}\n"
        target.write_bytes(foreign)

        with self.assertRaises(FileExistsError):
            install_hook.install_platform("opencode", opencode_config_dir=config)
        with self.assertRaises(FileExistsError):
            install_hook.uninstall_platform("opencode", opencode_config_dir=config)

        self.assertEqual(foreign, target.read_bytes())
        self.assertEqual("conflict", install_hook.integration_status("opencode", opencode_config_dir=config))

    def test_generated_file_uninstall_is_exact_and_does_not_need_backup(self):
        home = self.root / "pi"
        target = home / "extensions" / "pi-skill-analytics.ts"
        install_hook.install_platform("pi", pi_home=home)

        result = install_hook.uninstall_platform("pi", pi_home=home)

        self.assertEqual("removed", result["status"])
        self.assertFalse(target.exists())
        self.assertNotIn("backup", result)
        self.assertEqual("not_installed", install_hook.integration_status("pi", pi_home=home))

    def test_generated_file_update_backs_up_previous_owned_version(self):
        config = self.root / "opencode-update"
        target = config / "plugins" / "opencode-skill-analytics.js"
        target.parent.mkdir(parents=True)
        previous = (install_hook.GENERATED_SENTINEL + "// previous version\n").encode()
        target.write_bytes(previous)

        result = install_hook.install_platform(
            "opencode", opencode_config_dir=config
        )

        self.assertEqual("installed", result["status"])
        self.assertEqual(previous, Path(result["backup"]).read_bytes())
        self.assertNotEqual(previous, target.read_bytes())

    def test_rendered_literals_support_windows_and_posix_paths(self):
        template = "const pythonExecutable = __PYTHON_EXECUTABLE__\nconst projectRoot = __PROJECT_ROOT__\n"
        paths = (
            (PureWindowsPath(r"C:\\Program Files\\Python\\python.exe"), PureWindowsPath(r"D:\\Skill Analytics")),
            (PurePosixPath("/opt/python runtime/python3"), PurePosixPath("/srv/skill analytics")),
        )
        for executable, root in paths:
            rendered = install_hook._render_integration(template, executable, root)
            self.assertIn(json.dumps(str(executable)), rendered)
            self.assertIn(json.dumps(str(root)), rendered)

    def test_unknown_platform_is_rejected(self):
        for operation in (
            install_hook.install_platform,
            install_hook.uninstall_platform,
            install_hook.integration_status,
        ):
            with self.assertRaises(ValueError):
                operation("unknown")


if __name__ == "__main__":
    unittest.main()
