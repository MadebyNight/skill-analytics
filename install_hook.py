"""Install or remove the Skill analytics Codex Stop hook."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


HOOK_SCRIPT = Path(__file__).resolve().with_name("hook.py")
STATUS_MESSAGE = "Updating local Skill analytics"


def _codex_home(value: str | os.PathLike[str] | None = None) -> Path:
    return Path(value or os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()


def _quote_windows(value: str | os.PathLike[str]) -> str:
    return '"' + str(Path(value).resolve()).replace('"', '\\"') + '"'


def hook_definition(
    executable: str | os.PathLike[str] = sys.executable,
    hook_script: str | os.PathLike[str] = HOOK_SCRIPT,
) -> dict[str, list[dict[str, object]]]:
    return {
        "hooks": [
            {
                "type": "command",
                "command": f"{_quote_windows(executable)} {_quote_windows(hook_script)}",
                "statusMessage": STATUS_MESSAGE,
                "async": True,
            }
        ]
    }


def _read_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"hooks": {}}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("hooks.json must contain a JSON object")
    return value


def _stop_hooks(config: dict[str, object], *, create: bool) -> list[object] | None:
    hooks = config.get("hooks")
    if hooks is None and create:
        hooks = {}
        config["hooks"] = hooks
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be a JSON object")

    stop = hooks.get("Stop")
    if stop is None and create:
        stop = []
        hooks["Stop"] = stop
    if stop is None:
        return None
    if not isinstance(stop, list):
        raise ValueError("hooks.Stop must be a JSON array")
    return stop


def _write_config(path: Path, config: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _backup(path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.backup-{timestamp}")
    shutil.copy2(path, backup)
    return backup


def install(
    codex_home: str | os.PathLike[str] | None = None,
    *,
    executable: str | os.PathLike[str] = sys.executable,
    hook_script: str | os.PathLike[str] = HOOK_SCRIPT,
) -> tuple[bool, Path | None]:
    config_path = _codex_home(codex_home) / "hooks.json"
    config = _read_config(config_path)
    stop = _stop_hooks(config, create=True)
    assert stop is not None
    definition = hook_definition(executable, hook_script)
    if definition in stop:
        return False, None

    backup = _backup(config_path) if config_path.exists() else None
    stop.append(definition)
    _write_config(config_path, config)
    return True, backup


def uninstall(
    codex_home: str | os.PathLike[str] | None = None,
    *,
    executable: str | os.PathLike[str] = sys.executable,
    hook_script: str | os.PathLike[str] = HOOK_SCRIPT,
) -> bool:
    config_path = _codex_home(codex_home) / "hooks.json"
    if not config_path.exists():
        return False
    config = _read_config(config_path)
    stop = _stop_hooks(config, create=False)
    if stop is None:
        return False

    definition = hook_definition(executable, hook_script)
    remaining = [item for item in stop if item != definition]
    if len(remaining) == len(stop):
        return False
    hooks = config["hooks"]
    assert isinstance(hooks, dict)
    hooks["Stop"] = remaining
    _write_config(config_path, config)
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "uninstall"))
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "install":
        changed, backup = install()
        if changed:
            print(f"Hook installed. Backup: {backup}" if backup else "Hook installed.")
        else:
            print("Hook already installed.")
    else:
        print("Hook removed." if uninstall() else "Hook was not installed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
