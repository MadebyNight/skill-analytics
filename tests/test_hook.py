import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hook


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.error_log = Path(self.temp_dir.name) / "data" / "errors.log"

    def test_accepts_camel_case_fields_and_updates_report(self):
        payload = {
            "transcriptPath": "C:/sessions/example.jsonl",
            "sessionId": "session-1",
            "cwd": "C:/project",
            "model": "gpt-test",
        }
        with (
            mock.patch.object(hook.scanner, "scan_transcript") as scan,
            mock.patch.object(hook, "_generate_report") as report,
        ):
            result = hook.main(io.StringIO(json.dumps(payload)), error_log=self.error_log)

        self.assertEqual(result, 0)
        scan.assert_called_once_with(
            payload["transcriptPath"],
            session_id=payload["sessionId"],
            cwd=payload["cwd"],
            model=payload["model"],
            ingest_source="hook",
        )
        report.assert_called_once_with()
        self.assertFalse(self.error_log.exists())

    def test_accepts_snake_case_fields(self):
        payload = {
            "transcript_path": "C:/sessions/example.jsonl",
            "session_id": "session-2",
            "cwd": "C:/project",
            "model": "gpt-test",
        }
        with (
            mock.patch.object(hook.scanner, "scan_transcript") as scan,
            mock.patch.object(hook, "_generate_report"),
        ):
            result = hook.main(io.StringIO(json.dumps(payload)), error_log=self.error_log)

        self.assertEqual(result, 0)
        self.assertEqual(scan.call_args.kwargs["session_id"], "session-2")

    def test_legacy_hook_source_is_passed_to_the_compatible_scanner(self):
        payload = {"transcript_path": "C:/sessions/example.jsonl"}
        with (
            mock.patch.object(hook.scanner, "scan_transcript") as scan,
            mock.patch.object(hook, "_generate_report"),
        ):
            hook.main(io.StringIO(json.dumps(payload)), error_log=self.error_log)

        self.assertEqual("hook", scan.call_args.kwargs["ingest_source"])

    def test_invalid_input_is_logged_and_never_blocks(self):
        result = hook.main(io.StringIO("not-json"), error_log=self.error_log)

        self.assertEqual(result, 0)
        log = self.error_log.read_text(encoding="utf-8")
        self.assertIn("JSONDecodeError", log)
        self.assertNotIn("not-json", log)

    def test_scanner_failure_is_logged_and_report_is_not_called(self):
        payload = {"transcript_path": "C:/sessions/example.jsonl"}
        with (
            mock.patch.object(
                hook.scanner, "scan_transcript", side_effect=RuntimeError("database locked")
            ),
            mock.patch.object(hook, "_generate_report") as report,
        ):
            result = hook.main(io.StringIO(json.dumps(payload)), error_log=self.error_log)

        self.assertEqual(result, 0)
        report.assert_not_called()
        self.assertIn("RuntimeError: database locked", self.error_log.read_text(encoding="utf-8"))

    def test_report_failure_is_logged_and_never_blocks(self):
        payload = {"transcript_path": "C:/sessions/example.jsonl"}
        with (
            mock.patch.object(hook.scanner, "scan_transcript"),
            mock.patch.object(
                hook, "_generate_report", side_effect=OSError("dashboard unavailable")
            ),
        ):
            result = hook.main(io.StringIO(json.dumps(payload)), error_log=self.error_log)

        self.assertEqual(result, 0)
        self.assertIn("OSError: dashboard unavailable", self.error_log.read_text(encoding="utf-8"))

    def test_custom_database_and_output_are_used_by_hook(self):
        payload = {"transcript_path": "C:/sessions/example.jsonl"}
        database = Path(self.temp_dir.name) / "custom.sqlite"
        output = Path(self.temp_dir.name) / "custom.html"
        with (
            mock.patch.object(hook.scanner, "scan_transcript") as scan,
            mock.patch("report.generate_report") as report,
        ):
            result = hook.main(
                io.StringIO(json.dumps(payload)), error_log=self.error_log,
                db_path=database, output_path=output,
            )

        self.assertEqual(0, result)
        self.assertEqual(database, scan.call_args.kwargs["db_path"])
        report.assert_called_once_with(database, output)


if __name__ == "__main__":
    unittest.main()
