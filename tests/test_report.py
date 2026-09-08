import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import report
import scanner


PLATFORMS = ("codex", "claude", "opencode", "pi")


class ReportTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "analytics.db"
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE installed_skills (
                platform TEXT NOT NULL, skill_key TEXT NOT NULL, skill_name TEXT NOT NULL,
                skill_path TEXT, skill_source TEXT NOT NULL, first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL, PRIMARY KEY (platform, skill_key)
            );
            CREATE TABLE invocations (
                platform TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                skill_name TEXT NOT NULL, skill_path TEXT, skill_key TEXT NOT NULL,
                evidence_type TEXT NOT NULL, invoked_at TEXT NOT NULL, cwd TEXT,
                agent_kind TEXT NOT NULL, model TEXT, ingest_source TEXT NOT NULL,
                PRIMARY KEY (platform, session_id, turn_id, skill_key)
            );
            CREATE TABLE diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
                source_path TEXT NOT NULL, byte_offset INTEGER NOT NULL, line_number INTEGER,
                event_type TEXT, category TEXT NOT NULL, detail TEXT NOT NULL,
                adapter_version TEXT, format_version TEXT, created_at TEXT NOT NULL,
                UNIQUE (platform, source_path, byte_offset, category)
            );
            CREATE TABLE platform_status (
                platform TEXT PRIMARY KEY, status TEXT NOT NULL, resolved_root TEXT,
                last_history_scan_at TEXT, last_realtime_at TEXT, adapter_version TEXT,
                format_version TEXT, updated_at TEXT NOT NULL
            );
            """
        )
        for platform in PLATFORMS:
            status = "ready" if platform != "pi" else "not_installed"
            connection.execute(
                "INSERT INTO platform_status VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (platform, status, f"/{platform}",
                 "2026-09-07T06:00:00Z" if status == "ready" else None,
                 "2026-09-07T07:00:00Z" if platform == "claude" else None,
                 "1.0", "v1", "2026-09-07T07:00:00Z"),
            )
        connection.commit()
        connection.close()

    def _install(self, platform, key, name, path=None, source="user"):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "INSERT INTO installed_skills VALUES (?, ?, ?, ?, ?, ?, ?)",
                (platform, key, name, path, source,
                 "2026-01-01T00:00:00Z", "2026-09-07T00:00:00Z"),
            )

    def _invoke(self, platform, turn, name, invoked_at, *, key=None,
                evidence="structured_skill", agent="main", path=None):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "INSERT INTO invocations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (platform, f"{platform}-session", turn, name, path,
                 key or f"name:{name.casefold()}", evidence, invoked_at, "/work",
                 agent, "test-model", "history"),
            )

    def _collect(self):
        with mock.patch("report._refresh_codex_inventory"):
            return report.collect_report_data(
                self.db_path, now=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
                local_timezone=timezone.utc,
            )

    def test_legacy_report_refreshes_codex_inventory_from_all_override_roots(self):
        codex_home = self.root / "codex"
        agents_home = self.root / "agents"
        plugin_root = self.root / "custom-plugins"
        skill_paths = (
            codex_home / "skills" / "alpha" / "SKILL.md",
            agents_home / "skills" / "beta" / "SKILL.md",
            plugin_root / "market" / "plugin" / "1.0" / "skills" / "gamma" / "SKILL.md",
        )
        for path, name in zip(skill_paths, ("Alpha", "Beta", "Gamma")):
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

        data = report.collect_report_data(
            self.db_path,
            codex_home=codex_home,
            agents_home=agents_home,
            plugin_root=plugin_root,
            now=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
            local_timezone=timezone.utc,
        )

        self.assertEqual(3, data["views"]["codex"]["overview"]["installed_skills"])
        self.assertEqual({"Alpha", "Beta", "Gamma"}, {row["name"] for row in data["views"]["codex"]["never_used"]})

    def test_failed_codex_inventory_discovery_preserves_stored_inventory(self):
        self._install("codex", "saved", "Saved", "/saved/SKILL.md")
        with mock.patch("report.discover_skills", side_effect=OSError("unreadable")):
            data = report.collect_report_data(
                self.db_path,
                codex_home=self.root / "codex",
                agents_home=self.root / "agents",
                plugin_root=self.root / "plugins",
                now=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
                local_timezone=timezone.utc,
            )
        self.assertEqual(["Saved"], [row["name"] for row in data["views"]["codex"]["never_used"]])

    def test_single_platform_overview_counts_same_name_paths_by_skill_key(self):
        self._install("codex", "alpha-one", "Alpha", "/one/SKILL.md")
        self._install("codex", "alpha-two", "alpha", "/two/SKILL.md")
        self._invoke("codex", "one", "Alpha", "2026-09-07T01:00:00Z", key="alpha-one")

        data = self._collect()

        codex = data["views"]["codex"]
        self.assertEqual(
            {"installed_skills": 2, "used_skills": 1, "never_used_skills": 1},
            {key: codex["overview"][key] for key in ("installed_skills", "used_skills", "never_used_skills")},
        )
        self.assertEqual(1, data["views"]["all"]["overview"]["installed_skills"])
        self.assertEqual(1, data["views"]["all"]["overview"]["used_skills"])
        self.assertEqual(0, data["views"]["all"]["overview"]["never_used_skills"])

    def test_all_and_each_platform_views_merge_skills_by_name(self):
        self._install("codex", "/codex/alpha", "Alpha", "/codex/alpha/SKILL.md")
        self._install("claude", "/claude/alpha", "alpha", "/claude/alpha/SKILL.md")
        self._invoke("codex", "c1", "Alpha", "2026-09-01T01:00:00Z", key="/codex/alpha")
        self._invoke("claude", "a1", "alpha", "2026-09-02T01:00:00Z", key="/claude/alpha")
        self._invoke("opencode", "o1", "Beta", "2026-09-03T01:00:00Z")

        data = self._collect()

        self.assertEqual(["all", *PLATFORMS], list(data["views"]))
        self.assertEqual(3, data["views"]["all"]["overview"]["total_calls"])
        self.assertEqual(2, data["views"]["all"]["overview"]["used_skills"])
        alpha = next(row for row in data["views"]["all"]["ranking"] if row["name"].casefold() == "alpha")
        self.assertEqual(2, alpha["calls"])
        self.assertEqual({"codex": 1, "claude": 1}, alpha["platforms"])
        self.assertEqual(1, data["views"]["codex"]["overview"]["total_calls"])
        self.assertEqual(0, data["views"]["pi"]["overview"]["total_calls"])

    def test_trends_platform_agent_and_evidence_distributions(self):
        self._invoke("codex", "c1", "Alpha", "2026-08-31T23:00:00Z", agent="main", evidence="skill_file_read")
        self._invoke("claude", "a1", "Alpha", "2026-09-01T01:00:00Z", agent="subagent", evidence="slash_skill")
        self._invoke("opencode", "o1", "Beta", "2026-09-07T01:00:00Z", agent="unknown")

        view = self._collect()["views"]["all"]

        daily = {row["date"]: row for row in view["daily"]}
        self.assertEqual({"codex": 1}, daily["2026-08-31"]["platforms"])
        self.assertEqual({"claude": 1}, daily["2026-09-01"]["platforms"])
        weekly = {row["week"]: row for row in view["weekly"]}
        self.assertEqual(2, weekly["2026-08-31"]["count"])
        self.assertEqual(1, weekly["2026-09-07"]["count"])
        self.assertEqual(3, sum(row["count"] for row in view["weekly_detail"]))
        monthly = {row["month"]: row for row in view["monthly"]}
        self.assertEqual({"codex": 1}, monthly["2026-08"]["platforms"])
        self.assertEqual({"claude": 1, "opencode": 1}, monthly["2026-09"]["platforms"])
        agents = {row["platform"]: row for row in view["agent_by_platform"]}
        self.assertEqual(1, agents["codex"]["main"])
        self.assertEqual(1, agents["claude"]["subagent"])
        self.assertEqual(1, agents["opencode"]["unknown"])
        self.assertEqual(
            {"structured_skill": 1, "slash_skill": 1, "skill_file_read": 1},
            view["evidence_counts"],
        )
        self.assertEqual([{"cwd": "/work", "count": 3}], view["cwd_counts"])

    def test_never_and_stale_are_computed_per_platform_and_skill_key(self):
        self._install("codex", "alpha", "Alpha", "/codex/alpha")
        self._install("claude", "alpha", "Alpha", "/claude/alpha")
        self._install("claude", "old", "Old", "/claude/old")
        self._invoke("codex", "new", "Alpha", "2026-09-07T01:00:00Z", key="alpha")
        self._invoke("claude", "old", "Old", "2026-07-01T01:00:00Z", key="old")

        view = self._collect()["views"]["all"]

        self.assertEqual(
            [("claude", "Alpha")],
            [(row["platform"], row["name"]) for row in view["never_used"]],
        )
        self.assertEqual(
            [("claude", "Old")],
            [(row["platform"], row["name"]) for row in view["stale_30_days"]],
        )
        self.assertEqual([], self._collect()["views"]["codex"]["never_used"])

    def test_platform_status_and_diagnostics_are_exposed(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "INSERT INTO diagnostics "
                "(platform, source_path, byte_offset, category, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("claude", "transcript", 0, "unsupported_version", "v9", "2026-09-07T00:00:00Z"),
            )

        data = self._collect()

        statuses = {row["platform"]: row for row in data["platform_status"]}
        self.assertEqual("ready", statuses["codex"]["status"])
        self.assertEqual("2026-09-07T07:00:00Z", statuses["claude"]["last_realtime_at"])
        self.assertEqual("not_installed", statuses["pi"]["status"])
        self.assertEqual(1, data["views"]["all"]["quality"]["diagnostics"])
        self.assertEqual(1, data["views"]["claude"]["quality"]["diagnostics"])
        self.assertEqual(0, data["views"]["codex"]["quality"]["diagnostics"])

    def test_missing_platform_status_is_unknown_not_not_installed(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DELETE FROM platform_status WHERE platform = 'pi'")

        pi = next(row for row in self._collect()["platform_status"] if row["platform"] == "pi")

        self.assertEqual("unknown", pi["status"])
        self.assertIsNone(pi["updated_at"])

    def test_report_queries_share_one_explicit_read_snapshot(self):
        statements = []
        original = scanner.init_db

        def traced(path):
            connection = original(path)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch("report._refresh_codex_inventory"), mock.patch("report.scanner.init_db", side_effect=traced):
            report.collect_report_data(self.db_path)

        begin = next(index for index, sql in enumerate(statements) if sql == "BEGIN")
        selects = [index for index, sql in enumerate(statements) if sql.startswith("SELECT")]
        self.assertEqual(4, len(selects))
        self.assertTrue(all(index > begin for index in selects))

    def test_stale_includes_exactly_thirty_days_but_not_one_second_newer(self):
        self._install("codex", "boundary", "Boundary")
        self._install("codex", "newer", "Newer")
        self._invoke("codex", "boundary", "Boundary", "2026-08-08T12:00:00Z", key="boundary")
        self._invoke("codex", "newer", "Newer", "2026-08-08T12:00:01Z", key="newer")

        stale = self._collect()["views"]["codex"]["stale_30_days"]

        self.assertEqual(["Boundary"], [row["name"] for row in stale])

    def test_generated_html_is_offline_filterable_and_json_safe(self):
        dangerous = "</script><script>alert(1)</script>"
        self._invoke("codex", "x", dangerous, "2026-09-07T01:00:00Z")
        output = self.root / "dashboard.html"

        result = report.generate_report(
            self.db_path, output, now=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
            local_timezone=timezone.utc,
            codex_home=self.root / "empty-codex",
            agents_home=self.root / "empty-agents",
            plugin_root=self.root / "empty-plugins",
        )

        document = output.read_text(encoding="utf-8")
        self.assertEqual(output, result)
        self.assertIn('id="platform-filter"', document)
        for platform in ("all", *PLATFORMS):
            self.assertIn(f'value="{platform}"', document)
        self.assertIn("平台趋势", document)
        self.assertIn('id="skill-filter"', document)
        self.assertIn('id="agent-filter"', document)
        self.assertIn("平台 × 代理", document)
        self.assertIn("项目目录", document)
        self.assertIn("活跃天数", document)
        self.assertIn("item.active_skills", document)
        self.assertIn("item.change_percent", document)
        self.assertIn("aria-label", document)
        self.assertIn("tabIndex", document)
        self.assertIn("证据分布", document)
        self.assertIn("平台状态", document)
        self.assertNotIn("https://", document)
        self.assertNotIn("http://", document)
        payload = document.split('<script id="dashboard-data" type="application/json">', 1)[1].split("</script>", 1)[0]
        decoded = json.loads(payload)
        self.assertEqual(dangerous, decoded["views"]["all"]["ranking"][0]["name"])
        self.assertNotIn(dangerous, payload)

    def test_atomic_replace_failure_preserves_existing_report(self):
        output = self.root / "dashboard.html"
        output.write_text("old report", encoding="utf-8")

        with mock.patch("report.os.replace", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                report.generate_report(
                    self.db_path,
                    output,
                    codex_home=self.root / "empty-codex",
                    agents_home=self.root / "empty-agents",
                    plugin_root=self.root / "empty-plugins",
                )

        self.assertEqual("old report", output.read_text(encoding="utf-8"))
        self.assertEqual([], list(self.root.glob(".dashboard.html.*.tmp")))


if __name__ == "__main__":
    unittest.main()
