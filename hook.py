"""Non-blocking Codex Stop hook for incremental Skill analytics."""

from __future__ import annotations

import json
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import scanner


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ERROR_LOG = scanner.DATA_DIR / "errors.log"


def _generate_report(
    db_path: str | Path = scanner.DEFAULT_DB_PATH,
    output_path: str | Path | None = None,
) -> None:
    from report import generate_report

    if output_path is None:
        generate_report(db_path)
    else:
        generate_report(db_path, output_path)


def _value(payload: dict[str, object], snake_case: str, camel_case: str) -> object | None:
    return payload.get(snake_case, payload.get(camel_case))


def _log_error(error: BaseException, error_log: Path, transcript_path: object = None) -> None:
    try:
        error_log.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        location = str(transcript_path) if transcript_path else "unknown transcript"
        summary = str(error).replace("\r", " ").replace("\n", " ")
        with error_log.open("a", encoding="utf-8") as stream:
            stream.write(f"{timestamp}\t{location}\t{type(error).__name__}: {summary}\n")
    except BaseException:
        pass


def main(
    stdin: TextIO | None = None,
    *,
    error_log: Path = DEFAULT_ERROR_LOG,
    db_path: str | Path = scanner.DEFAULT_DB_PATH,
    output_path: str | Path | None = None,
) -> int:
    transcript_path: object = None
    try:
        payload = json.load(stdin or sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")

        transcript_path = _value(payload, "transcript_path", "transcriptPath")
        if not isinstance(transcript_path, str) or not transcript_path:
            raise ValueError("hook input is missing transcript_path")

        session_id = _value(payload, "session_id", "sessionId")
        cwd = payload.get("cwd")
        model = payload.get("model")
        scan_options = {
            "session_id": str(session_id) if session_id is not None else None,
            "cwd": str(cwd) if cwd is not None else None,
            "model": str(model) if model is not None else None,
            "ingest_source": "hook",
        }
        if Path(db_path).resolve() != Path(scanner.DEFAULT_DB_PATH).resolve():
            scan_options["db_path"] = db_path
        scanner.scan_transcript(
            transcript_path,
            **scan_options,
        )
        if (
            Path(db_path).resolve() == Path(scanner.DEFAULT_DB_PATH).resolve()
            and output_path is None
        ):
            _generate_report()
        else:
            _generate_report(db_path, output_path)
    except BaseException as error:
        _log_error(error, Path(error_log), transcript_path)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=scanner.DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path)
    return parser


if __name__ == "__main__":
    arguments = _parser().parse_args()
    main(db_path=arguments.db, output_path=arguments.output)
