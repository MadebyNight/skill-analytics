"""Non-blocking Codex Stop hook for incremental Skill analytics."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import scanner


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ERROR_LOG = PROJECT_ROOT / "data" / "errors.log"


def _generate_report() -> None:
    from report import generate_report

    generate_report()


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


def main(stdin: TextIO | None = None, *, error_log: Path = DEFAULT_ERROR_LOG) -> int:
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
        scanner.scan_transcript(
            transcript_path,
            session_id=str(session_id) if session_id is not None else None,
            cwd=str(cwd) if cwd is not None else None,
            model=str(model) if model is not None else None,
            ingest_source="hook",
        )
        _generate_report()
    except BaseException as error:
        _log_error(error, Path(error_log), transcript_path)
    return 0


if __name__ == "__main__":
    main()
