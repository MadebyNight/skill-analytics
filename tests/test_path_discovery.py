import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

from adapters import ClaudeAdapter, CodexAdapter, OpenCodeAdapter, PiAdapter


PATH_CASES = (
    (
        "windows",
        PureWindowsPath,
        r"C:\Users\Ada Lovelace",
        r"C:\Users\Ada Lovelace\AppData\Roaming\OpenCode",
    ),
    (
        "macos",
        PurePosixPath,
        "/Users/Ada Lovelace",
        "/Users/Ada Lovelace/Library/Application Support/OpenCode",
    ),
    (
        "linux",
        PurePosixPath,
        "/home/ada",
        "/home/ada/.config/opencode",
    ),
)

PLATFORM_ENV = {
    "CODEX_HOME": "",
    "CLAUDE_CONFIG_DIR": "",
    "OPENCODE_CONFIG_DIR": "",
    "PI_CODING_AGENT_DIR": "",
    "PI_CODING_AGENT_SESSION_DIR": "",
}


class CrossPlatformPathDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _write_skill(path: Path, name: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {name}\ndescription: path fixture\n---\n",
            encoding="utf-8",
        )
        return path.resolve()

    @staticmethod
    def _write_session(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        return path

    def test_native_path_fixtures_preserve_drive_root_and_spaces(self):
        for system, path_type, home_value, config_value in PATH_CASES:
            with self.subTest(system=system):
                home = path_type(home_value)
                config = path_type(config_value)
                codex_session = home / ".codex" / "sessions" / "one.jsonl"
                claude_skill = home / ".claude" / "skills" / "alpha" / "SKILL.md"
                pi_sessions = home / ".pi" / "agent" / "sessions"

                self.assertTrue(home.is_absolute())
                self.assertTrue(config.is_absolute())
                self.assertEqual(home / ".codex" / "sessions", codex_session.parent)
                self.assertEqual(home / ".claude" / "skills" / "alpha", claude_skill.parent)
                self.assertEqual(home / ".pi" / "agent", pi_sessions.parent)
                self.assertEqual(".jsonl", codex_session.suffix)
                self.assertEqual("SKILL.md", claude_skill.name)
                self.assertEqual("opencode", config.name.casefold())
                if system == "windows":
                    self.assertEqual("C:\\", home.anchor)
                    self.assertEqual("C:", home.drive)
                    self.assertIn("Ada Lovelace", home.parts)
                else:
                    self.assertEqual("/", home.anchor)

    def test_default_roots_and_core_skill_session_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            for system, _, _, _ in PATH_CASES:
                with self.subTest(system=system):
                    mapped = fixture_root / system
                    home = mapped / "Users" / "Ada Lovelace"
                    project = mapped / "Work Trees" / "analytics"
                    cwd = project / "nested"
                    cwd.mkdir(parents=True)
                    (project / ".git").mkdir()

                    codex_skill = self._write_skill(
                        home / ".codex" / "skills" / "codex-default" / "SKILL.md",
                        "codex-default",
                    )
                    agents_skill = self._write_skill(
                        home / ".agents" / "skills" / "agents-default" / "SKILL.md",
                        "agents-default",
                    )
                    claude_skill = self._write_skill(
                        home / ".claude" / "skills" / "claude-default" / "SKILL.md",
                        "claude-default",
                    )
                    claude_project_skill = self._write_skill(
                        project / ".claude" / "skills" / "claude-project" / "SKILL.md",
                        "claude-project",
                    )
                    opencode_skill = self._write_skill(
                        home
                        / ".config"
                        / "opencode"
                        / "skills"
                        / "opencode-default"
                        / "SKILL.md",
                        "opencode-default",
                    )
                    opencode_project_skill = self._write_skill(
                        project / ".opencode" / "skills" / "opencode-project" / "SKILL.md",
                        "opencode-project",
                    )
                    pi_skill = self._write_skill(
                        home / ".pi" / "agent" / "skills" / "pi-default" / "SKILL.md",
                        "pi-default",
                    )
                    pi_project_skill = self._write_skill(
                        project / ".pi" / "skills" / "pi-project" / "SKILL.md",
                        "pi-project",
                    )
                    codex_session = self._write_session(
                        home / ".codex" / "sessions" / "codex.jsonl"
                    )
                    claude_session = self._write_session(
                        home / ".claude" / "projects" / "fixture" / "claude.jsonl"
                    )
                    pi_session = self._write_session(
                        home / ".pi" / "agent" / "sessions" / "pi.jsonl"
                    )

                    with mock.patch.dict(os.environ, PLATFORM_ENV), mock.patch(
                        "pathlib.Path.home", return_value=home
                    ), mock.patch("adapters.opencode.subprocess.run") as run:
                        adapters = {
                            "codex": CodexAdapter(),
                            "claude": ClaudeAdapter(project_dirs=[cwd]),
                            "opencode": OpenCodeAdapter(cwd=cwd, command=mapped / "opencode"),
                            "pi": PiAdapter(cwd=cwd),
                        }

                        self.assertEqual((home / ".codex").resolve(), adapters["codex"].resolved_root)
                        self.assertEqual((home / ".claude").resolve(), adapters["claude"].resolved_root)
                        self.assertEqual(
                            (home / ".config" / "opencode").resolve(),
                            adapters["opencode"].resolved_root,
                        )
                        self.assertEqual(
                            (home / ".pi" / "agent").resolve(), adapters["pi"].resolved_root
                        )
                        self.assertEqual([codex_session], adapters["codex"].discover_transcripts())
                        self.assertEqual([claude_session], adapters["claude"].discover_transcripts())
                        self.assertEqual([pi_session], adapters["pi"].discover_transcripts())

                        expected_skills = {
                            "codex": {codex_skill, agents_skill},
                            "claude": {claude_skill, claude_project_skill},
                            "opencode": {
                                opencode_skill,
                                opencode_project_skill,
                                claude_skill,
                                claude_project_skill,
                                agents_skill,
                            },
                            "pi": {pi_skill, pi_project_skill, agents_skill},
                        }
                        for platform, adapter in adapters.items():
                            discovered = {
                                Path(skill.skill_path) for skill in adapter.discover_installed_skills()
                            }
                            self.assertTrue(expected_skills[platform].issubset(discovered))
                        run.assert_not_called()

    def test_environment_and_explicit_overrides_win_for_every_path_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory)
            for system, _, _, _ in PATH_CASES:
                with self.subTest(system=system):
                    mapped = fixture_root / system
                    home = mapped / "Default Home"
                    environment = mapped / "Environment Config"
                    explicit = mapped / "Explicit Config"
                    cwd = mapped / "Work Trees" / "analytics"
                    environment_values = {
                        "CODEX_HOME": str(environment / "codex"),
                        "CLAUDE_CONFIG_DIR": str(environment / "claude"),
                        "OPENCODE_CONFIG_DIR": str(environment / "opencode"),
                        "PI_CODING_AGENT_DIR": str(environment / "pi"),
                        "PI_CODING_AGENT_SESSION_DIR": str(environment / "pi-sessions"),
                    }
                    with mock.patch.dict(os.environ, environment_values), mock.patch(
                        "pathlib.Path.home", return_value=home
                    ):
                        environment_adapters = {
                            "codex": CodexAdapter(),
                            "claude": ClaudeAdapter(project_dirs=[cwd]),
                            "opencode": OpenCodeAdapter(
                                cwd=cwd, command=mapped / "opencode"
                            ),
                            "pi": PiAdapter(cwd=cwd),
                        }
                        explicit_adapters = {
                            "codex": CodexAdapter(codex_home=explicit / "codex"),
                            "claude": ClaudeAdapter(
                                claude_home=explicit / "claude", project_dirs=[cwd]
                            ),
                            "opencode": OpenCodeAdapter(
                                config_dir=explicit / "opencode",
                                cwd=cwd,
                                command=mapped / "opencode",
                            ),
                            "pi": PiAdapter(
                                pi_home=explicit / "pi",
                                session_dir=explicit / "pi-sessions",
                                cwd=cwd,
                            ),
                        }

                    for platform in environment_adapters:
                        self.assertEqual(
                            (environment / platform).resolve(),
                            environment_adapters[platform].resolved_root,
                        )
                        self.assertEqual(
                            (explicit / platform).resolve(), explicit_adapters[platform].resolved_root
                        )
                    self.assertEqual(
                        (environment / "pi-sessions").resolve(),
                        environment_adapters["pi"].resolved_session_dir,
                    )
                    self.assertEqual(
                        (explicit / "pi-sessions").resolve(),
                        explicit_adapters["pi"].resolved_session_dir,
                    )


if __name__ == "__main__":
    unittest.main()
