import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import report
import scanner


class ReportTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "analytics.db"
        self.codex_home = self.root / ".codex"
        self.agents_home = self.root / ".agents"
        self.plugin_root = self.codex_home / "plugins" / "cache"
        self.alpha = self._skill(self.codex_home / "skills" / "alpha", "Alpha")
        self.system = self._skill(
            self.codex_home / "skills" / ".system" / "system-skill", "System Skill"
        )
        self.beta = self._skill(self.agents_home / "skills" / "beta", None)
        self.gamma = self._skill(
            self.plugin_root
            / "openai-primary-runtime"
            / "gamma-plugin"
            / "2.0.0"
            / "skills"
            / "gamma",
            "Gamma",
        )

        connection = scanner.init_db(self.db_path)
        connection.close()

    @staticmethod
    def _skill(directory, name):
        directory.mkdir(parents=True)
        path = directory / "SKILL.md"
        frontmatter = f"---\nname: {name}\n---\n" if name else "No frontmatter\n"
        path.write_text(frontmatter, encoding="utf-8")
        return path.resolve()

    def _insert_invocation(self, turn, skill, invoked_at, agent="main", cwd="/work/one"):
        skill_path = os.path.normcase(str(skill))
        connection = sqlite3.connect(self.db_path)
        try:
            name = report._skill_name(skill)
            connection.execute(
                "INSERT OR IGNORE INTO skills VALUES (?, ?, ?, ?, ?)",
                (skill_path, name, "other", invoked_at, invoked_at),
            )
            connection.execute(
                "INSERT INTO invocations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("session", turn, skill_path, invoked_at, cwd, agent, "gpt-test", "history"),
            )
            connection.commit()
        finally:
            connection.close()

    def _collect(self):
        return report.collect_report_data(
            db_path=self.db_path,
            codex_home=self.codex_home,
            agents_home=self.agents_home,
            plugin_root=self.plugin_root,
            now=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
            local_timezone=timezone.utc,
        )

    def test_installed_skills_are_refreshed_and_never_used_is_visible(self):
        self._insert_invocation("one", self.alpha, "2026-09-07T04:00:00Z")

        data = self._collect()

        self.assertEqual(4, data["overview"]["installed_skills"])
        self.assertEqual(1, data["overview"]["used_skills"])
        self.assertEqual(3, data["overview"]["never_used_skills"])
        self.assertEqual(
            {"System Skill", "beta", "Gamma"},
            {skill["name"] for skill in data["never_used"]},
        )
        sources = {skill["name"]: skill["source"] for skill in data["never_used"]}
        self.assertEqual("system", sources["System Skill"])
        self.assertEqual("agents", sources["beta"])
        self.assertEqual("plugin", sources["Gamma"])

    def test_discovery_uses_current_plugin_version_and_deduplicates_by_name_priority(self):
        self._skill(
            self.plugin_root
            / "openai-primary-runtime"
            / "gamma-plugin"
            / "1.0.0"
            / "skills"
            / "old-gamma",
            "Legacy Gamma",
        )
        self._skill(
            self.plugin_root
            / "openai-bundled"
            / "plugin-backup-old"
            / "gamma-plugin"
            / "9.0.0"
            / "skills"
            / "backup-gamma",
            "Backup Gamma",
        )
        agents_alpha = self._skill(self.agents_home / "skills" / "alpha-copy", "Alpha")
        self._skill(
            self.plugin_root
            / "openai-primary-runtime"
            / "alpha-plugin"
            / "1.0.0"
            / "skills"
            / "alpha-copy",
            "Alpha",
        )

        skills = report.discover_skills(
            self.codex_home, self.agents_home, self.plugin_root
        )

        self.assertEqual(1, sum(skill["name"] == "Alpha" for skill in skills))
        alpha = next(skill for skill in skills if skill["name"] == "Alpha")
        self.assertEqual("other", alpha["source"])
        self.assertEqual(str(self.alpha).casefold(), alpha["path"].casefold())
        self.assertEqual(1, sum(skill["name"] == "Gamma" for skill in skills))
        self.assertNotIn("Legacy Gamma", {skill["name"] for skill in skills})
        self.assertNotIn("Backup Gamma", {skill["name"] for skill in skills})

        self._insert_invocation("agents-alpha", agents_alpha, "2026-09-07T04:00:00Z")
        data = self._collect()
        self.assertNotIn("Alpha", {skill["name"] for skill in data["never_used"]})
        alpha_usage = next(skill for skill in data["ranking"] if skill["name"] == "Alpha")
        self.assertTrue(alpha_usage["installed"])
        self.assertEqual(str(self.alpha).casefold(), alpha_usage["path"].casefold())

    def test_daily_weekly_monthly_agent_cwd_and_ranking_aggregates(self):
        self._insert_invocation("one", self.alpha, "2026-08-31T23:30:00Z", "main")
        self._insert_invocation("two", self.alpha, "2026-09-01T00:30:00Z", "subagent")
        self._insert_invocation("three", self.alpha, "2026-09-07T04:00:00Z", "unknown", "/work/two")

        data = self._collect()

        daily = {row["date"]: row["count"] for row in data["daily"]}
        self.assertEqual(1, daily["2026-08-31"])
        self.assertEqual(1, daily["2026-09-01"])
        self.assertEqual(1, daily["2026-09-07"])
        weekly = {row["week"]: row["count"] for row in data["weekly"]}
        self.assertEqual(2, weekly["2026-08-31"])
        self.assertEqual(1, weekly["2026-09-07"])
        monthly = {row["month"]: row for row in data["monthly"]}
        self.assertEqual(1, monthly["2026-08"]["count"])
        self.assertEqual(2, monthly["2026-09"]["count"])
        self.assertEqual(100.0, monthly["2026-09"]["change_percent"])
        self.assertEqual(
            {"main": 1, "subagent": 1, "unknown": 1}, data["agent_counts"]
        )
        self.assertEqual("Alpha", data["ranking"][0]["name"])
        self.assertEqual(3, data["ranking"][0]["calls"])
        self.assertEqual(3, data["ranking"][0]["active_days"])
        self.assertEqual("2026-09-07T04:00:00Z", data["ranking"][0]["last_invoked_at"])
        self.assertEqual(2, data["cwd_counts"][0]["count"])

    def test_stale_and_diagnostic_quality_sections(self):
        self._insert_invocation("old", self.alpha, "2026-07-01T00:00:00Z")
        connection = sqlite3.connect(self.db_path)
        try:
            rows = [
                ("bad", "invalid_json"),
                ("encoding", "invalid_encoding"),
                ("agent", "unknown_agent"),
                ("scan", "scan_failure"),
            ]
            for offset, (detail, category) in enumerate(rows):
                connection.execute(
                    "INSERT INTO diagnostics "
                    "(transcript_path, byte_offset, line_number, event_type, category, detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("session.jsonl", offset, offset + 1, None, category, detail, "2026-09-07T00:00:00Z"),
                )
            connection.commit()
        finally:
            connection.close()

        data = self._collect()

        self.assertEqual(["Alpha"], [skill["name"] for skill in data["stale_30_days"]])
        self.assertEqual(2, data["quality"]["unparsed_events"])
        self.assertEqual(1, data["quality"]["unknown_agents"])
        self.assertEqual(1, data["quality"]["scan_failures"])

    def test_generated_html_has_static_summary_and_embedded_data(self):
        self._insert_invocation("one", self.alpha, "2026-09-07T04:00:00Z")
        output = self.root / "dashboard.html"

        result = report.generate_report(
            db_path=self.db_path,
            output_path=output,
            codex_home=self.codex_home,
            agents_home=self.agents_home,
            plugin_root=self.plugin_root,
            now=datetime(2026, 9, 7, 12, tzinfo=timezone.utc),
            local_timezone=timezone.utc,
        )

        html = output.read_text(encoding="utf-8")
        self.assertEqual(output, result)
        self.assertIn("日视图", html)
        self.assertIn("周视图", html)
        self.assertIn("月视图", html)
        self.assertIn("从未使用", html)
        self.assertIn("数据质量", html)
        self.assertIn("活跃 Skill", html)
        self.assertIn("环比", html)
        self.assertIn("item.active_skills", html)
        self.assertIn("item.change_percent", html)
        self.assertIn("Alpha", html)
        payload = html.split('<script id="dashboard-data" type="application/json">', 1)[1]
        payload = payload.split("</script>", 1)[0]
        self.assertEqual(1, json.loads(payload)["overview"]["total_calls"])


if __name__ == "__main__":
    unittest.main()
