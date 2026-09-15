"""Generate a self-contained local dashboard from Skill invocation data."""

from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable

import scanner
from adapters.base import InstalledSkill
from adapters.codex import CodexAdapter


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = scanner.DEFAULT_DB_PATH
DEFAULT_OUTPUT_PATH = scanner.DATA_DIR / "dashboard.html"
DEFAULT_TEMPLATE_PATH = PROJECT_ROOT / "templates" / "dashboard.html"
PLATFORMS = ("codex", "claude", "opencode", "pi")
VIEW_NAMES = ("all", *PLATFORMS)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def discover_skills(
    codex_home: str | os.PathLike[str] | None = None,
    agents_home: str | os.PathLike[str] | None = None,
    plugin_root: str | os.PathLike[str] | None = None,
) -> list[dict[str, str]]:
    """Compatibility wrapper for the original report-side Codex inventory API."""
    adapter = CodexAdapter(
        codex_home=codex_home,
        agents_home=agents_home,
        plugin_root=plugin_root,
    )
    return [
        {"path": skill.skill_path or "", "name": skill.skill_name, "source": skill.skill_source}
        for skill in adapter.discover_installed_skills()
    ]


def _refresh_codex_inventory(
    db_path: str | os.PathLike[str],
    codex_home: str | os.PathLike[str] | None,
    agents_home: str | os.PathLike[str] | None,
    plugin_root: str | os.PathLike[str] | None,
    seen_at: str,
) -> bool:
    """Replace Codex inventory only when every configured root was enumerated."""
    try:
        discovered = discover_skills(codex_home, agents_home, plugin_root)
    except (OSError, UnicodeError):
        return False
    inventory = [
        InstalledSkill(
            platform="codex",
            skill_key=skill["path"],
            skill_name=skill["name"],
            skill_path=skill["path"],
            skill_source=skill["source"],
            first_seen_at=seen_at,
            last_seen_at=seen_at,
        )
        for skill in discovered
    ]
    scanner.replace_installed_skills("codex", inventory, db_path, complete=True)
    return True


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _local_datetime(value: str, local_timezone: tzinfo | None) -> datetime | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return parsed.astimezone(local_timezone) if local_timezone is not None else parsed.astimezone()


def _month_keys(today: datetime, count: int = 12) -> list[str]:
    index = today.year * 12 + today.month - 1
    keys = []
    for offset in range(count - 1, -1, -1):
        month_index = index - offset
        year, month_zero = divmod(month_index, 12)
        keys.append(f"{year:04d}-{month_zero + 1:02d}")
    return keys


def collect_report_data(
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    *,
    codex_home: str | os.PathLike[str] | None = None,
    agents_home: str | os.PathLike[str] | None = None,
    plugin_root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
    local_timezone: tzinfo | None = None,
) -> dict[str, Any]:
    """Return unified, presentation-ready aggregates for every platform filter."""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=local_timezone or timezone.utc)
    _refresh_codex_inventory(
        db_path, codex_home, agents_home, plugin_root, _iso_utc(current)
    )
    connection = scanner.init_db(db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        invocation_rows = connection.execute(
            "SELECT * FROM invocations ORDER BY invoked_at"
        ).fetchall()
        installed_rows = connection.execute(
            "SELECT * FROM installed_skills ORDER BY platform, skill_name, skill_key"
        ).fetchall()
        diagnostic_rows = connection.execute(
            "SELECT platform, category, COUNT(*) AS count FROM diagnostics "
            "GROUP BY platform, category"
        ).fetchall()
        status_rows = connection.execute(
            "SELECT * FROM platform_status ORDER BY platform"
        ).fetchall()
    finally:
        connection.close()

    invocations = [dict(row) for row in invocation_rows]
    installed = [dict(row) for row in installed_rows]
    diagnostics = [dict(row) for row in diagnostic_rows]
    status_by_platform = {row["platform"]: dict(row) for row in status_rows}
    platform_status = []
    for platform in PLATFORMS:
        platform_status.append(
            status_by_platform.get(
                platform,
                {
                    "platform": platform,
                    "status": "unknown",
                    "resolved_root": None,
                    "last_history_scan_at": None,
                    "last_realtime_at": None,
                    "adapter_version": None,
                    "format_version": None,
                    "updated_at": None,
                },
            )
        )

    views = {
        name: _build_view(name, invocations, installed, diagnostics, current, local_timezone)
        for name in VIEW_NAMES
    }
    platform_summaries = []
    for platform in PLATFORMS:
        overview = views[platform]["overview"]
        platform_summaries.append(
            {
                "platform": platform,
                "calls": overview["total_calls"],
                "used_skills": overview["used_skills"],
                "installed_skills": overview["installed_skills"],
                "status": status_by_platform.get(platform, {}).get("status", "unknown"),
            }
        )
    result = {
        "generated_at": _iso_utc(current),
        "platforms": list(VIEW_NAMES),
        "platform_summaries": platform_summaries,
        "platform_status": platform_status,
        "views": views,
    }
    # Preserve the original Python data-access shape for report.py callers.
    result.update(views["all"])
    return result


def _build_view(
    selected: str,
    all_invocations: list[dict[str, Any]],
    all_installed: list[dict[str, Any]],
    all_diagnostics: list[dict[str, Any]],
    current: datetime,
    local_timezone: tzinfo | None,
) -> dict[str, Any]:
    selected_platforms = set(PLATFORMS if selected == "all" else (selected,))
    invocation_rows = [row for row in all_invocations if row["platform"] in selected_platforms]
    installed_rows = [row for row in all_installed if row["platform"] in selected_platforms]
    daily_counts: Counter[str] = Counter()
    weekly_counts: Counter[str] = Counter()
    monthly_counts: Counter[str] = Counter()
    daily_platforms: dict[str, Counter[str]] = defaultdict(Counter)
    weekly_platforms: dict[str, Counter[str]] = defaultdict(Counter)
    monthly_platforms: dict[str, Counter[str]] = defaultdict(Counter)
    monthly_skills: dict[str, set[str]] = defaultdict(set)
    agent_counts: dict[str, Counter[str]] = defaultdict(Counter)
    evidence_counts: Counter[str] = Counter()
    cwd_counts: Counter[str] = Counter()
    per_skill: dict[str, dict[str, Any]] = {}
    weekly_detail_counts: Counter[tuple[str, str, str, str]] = Counter()
    used_installations: set[tuple[str, str]] = set()
    last_by_installation: dict[tuple[str, str], str] = {}

    for row in invocation_rows:
        platform = row["platform"]
        install_identity = (platform, row["skill_key"])
        used_installations.add(install_identity)
        previous = last_by_installation.get(install_identity)
        if previous is None or _timestamp_sort_key(row["invoked_at"]) > _timestamp_sort_key(previous):
            last_by_installation[install_identity] = row["invoked_at"]
        local = _local_datetime(row["invoked_at"], local_timezone)
        logical_name = str(row["skill_name"]).casefold()
        identity = logical_name if selected == "all" else row["skill_key"]
        if local is not None:
            day = local.date().isoformat()
            week = (local.date() - timedelta(days=local.weekday())).isoformat()
            month = day[:7]
            daily_counts[day] += 1
            weekly_counts[week] += 1
            monthly_counts[month] += 1
            daily_platforms[day][platform] += 1
            weekly_platforms[week][platform] += 1
            monthly_platforms[month][platform] += 1
            monthly_skills[month].add(identity)
            weekly_detail_counts[(week, identity, row["agent_kind"], platform)] += 1
        agent_counts[platform][row["agent_kind"]] += 1
        evidence_counts[row["evidence_type"]] += 1
        cwd_counts[row["cwd"] or "未知目录"] += 1

        summary = per_skill.setdefault(
            identity,
            {
                "name": row["skill_name"],
                "calls": 0,
                "days": set(),
                "last_invoked_at": row["invoked_at"],
                "platforms": Counter(),
            },
        )
        summary["calls"] += 1
        if local is not None:
            summary["days"].add(local.date().isoformat())
        summary["platforms"][platform] += 1
        if _timestamp_sort_key(row["invoked_at"]) > _timestamp_sort_key(summary["last_invoked_at"]):
            summary["last_invoked_at"] = row["invoked_at"]

    installations_by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in installed_rows:
        identity = row["skill_name"].casefold() if selected == "all" else row["skill_key"]
        installations_by_identity[identity].append(
            {
                "platform": row["platform"],
                "key": row["skill_key"],
                "path": row["skill_path"],
                "source": row["skill_source"],
            }
        )
    ranking = []
    for identity, item in per_skill.items():
        ranking.append(
            {
                "identity": identity,
                "name": item["name"],
                "calls": item["calls"],
                "active_days": len(item["days"]),
                "last_invoked_at": item["last_invoked_at"],
                "platforms": dict(item["platforms"]),
                "installed": identity in installations_by_identity,
                "installations": installations_by_identity.get(identity, []),
            }
        )
    ranking.sort(key=lambda item: (-item["calls"], item["name"].casefold()))

    manageable_rows = [row for row in installed_rows if row["skill_source"] != "system"]
    never_used = [
        _installed_item(row)
        for row in manageable_rows
        if (row["platform"], row["skill_key"]) not in used_installations
    ]
    never_used.sort(key=lambda row: (row["platform"], row["name"].casefold(), row["key"]))
    local_current = current.astimezone(local_timezone) if local_timezone else current.astimezone()
    stale = []
    stale_cutoff = current.astimezone(timezone.utc) - timedelta(days=30)
    for row in installed_rows:
        last = last_by_installation.get((row["platform"], row["skill_key"]))
        parsed_last = _parse_timestamp(last) if last else None
        if parsed_last is not None and parsed_last.astimezone(timezone.utc) <= stale_cutoff:
            item = _installed_item(row)
            item["last_invoked_at"] = last
            stale.append(item)
    stale.sort(key=lambda row: (row["platform"], row["name"].casefold(), row["key"]))

    today = local_current
    start_day = today.date() - timedelta(days=364)
    daily = [
        _period_row("date", (start_day + timedelta(days=offset)).isoformat(), daily_counts, daily_platforms)
        for offset in range(365)
    ]
    current_week = today.date() - timedelta(days=today.weekday())
    week_keys = [(current_week - timedelta(weeks=offset)).isoformat() for offset in range(11, -1, -1)]
    weekly = [_period_row("week", key, weekly_counts, weekly_platforms) for key in week_keys]
    month_keys = _month_keys(today)
    monthly = []
    for index, key in enumerate(month_keys):
        count = monthly_counts[key]
        previous = monthly_counts[month_keys[index - 1]] if index else 0
        change = round((count - previous) * 100 / previous, 1) if previous else (0.0 if count == 0 else None)
        monthly.append(
            {"month": key, "count": count, "platforms": dict(monthly_platforms[key]),
             "active_skills": len(monthly_skills[key]), "change_percent": change}
        )

    weekly_detail = [
        {"week": week, "skill_name": skill_name, "agent": agent,
         "platform": platform, "count": count}
        for (week, skill_name, agent, platform), count in sorted(weekly_detail_counts.items())
        if week in week_keys
    ]

    active_days = len({day for item in per_skill.values() for day in item["days"]})
    diagnostic_counts: Counter[str] = Counter()
    for row in all_diagnostics:
        if row["platform"] in selected_platforms:
            diagnostic_counts[row["category"]] += row["count"]
    installed_identities = {(row["platform"], row["skill_key"]) for row in installed_rows}
    if selected == "all":
        installed_names = {row["skill_name"].casefold() for row in installed_rows}
        installed_skill_count = len(installed_names)
        used_skill_count = len(per_skill)
        manageable_names = {row["skill_name"].casefold() for row in manageable_rows}
        manageable_used_names = {
            row["skill_name"].casefold()
            for row in manageable_rows
            if (row["platform"], row["skill_key"]) in used_installations
        }
        never_used_skill_count = len(manageable_names - manageable_used_names)
    else:
        installed_skill_count = len(installed_identities)
        used_skill_count = len(used_installations)
        never_used_skill_count = len(never_used)
    return {
        "overview": {
            "total_calls": len(invocation_rows),
            "installed_skills": installed_skill_count,
            "used_skills": used_skill_count,
            "never_used_skills": never_used_skill_count,
            "active_days": active_days,
        },
        "daily": daily,
        "weekly": weekly,
        "weekly_detail": weekly_detail,
        "monthly": monthly,
        "agent_counts": {
            kind: sum(counts[kind] for counts in agent_counts.values())
            for kind in ("main", "subagent", "unknown")
        },
        "agent_by_platform": [
            {"platform": platform, **{kind: agent_counts[platform][kind] for kind in ("main", "subagent", "unknown")}}
            for platform in PLATFORMS if platform in selected_platforms
        ],
        "evidence_counts": {
            kind: evidence_counts[kind]
            for kind in ("structured_skill", "slash_skill", "skill_file_read")
        },
        "cwd_counts": [
            {"cwd": cwd, "count": count}
            for cwd, count in sorted(cwd_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "ranking": ranking,
        "never_used": never_used,
        "stale_30_days": stale,
        "quality": {
            "diagnostics": sum(diagnostic_counts.values()),
            "unparsed_events": diagnostic_counts.get("invalid_json", 0)
            + diagnostic_counts.get("invalid_encoding", 0),
            "unknown_agents": diagnostic_counts.get("unknown_agent", 0),
            "scan_failures": diagnostic_counts.get("scan_failure", 0),
        },
    }


def _timestamp_sort_key(value: str) -> datetime:
    return _parse_timestamp(value) or datetime.min.replace(tzinfo=timezone.utc)


def _installed_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": row["platform"],
        "key": row["skill_key"],
        "name": row["skill_name"],
        "path": row["skill_path"],
        "source": row["skill_source"],
    }


def _period_row(
    label: str,
    key: str,
    counts: Counter[str],
    platform_counts: dict[str, Counter[str]],
) -> dict[str, Any]:
    return {label: key, "count": counts[key], "platforms": dict(platform_counts[key])}


def generate_report(
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    output_path: str | os.PathLike[str] = DEFAULT_OUTPUT_PATH,
    *,
    template_path: str | os.PathLike[str] = DEFAULT_TEMPLATE_PATH,
    codex_home: str | os.PathLike[str] | None = None,
    agents_home: str | os.PathLike[str] | None = None,
    plugin_root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
    local_timezone: tzinfo | None = None,
) -> Path:
    data = collect_report_data(
        db_path,
        codex_home=codex_home,
        agents_home=agents_home,
        plugin_root=plugin_root,
        now=now,
        local_timezone=local_timezone,
    )
    template = Path(template_path).read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for source, escaped in (
        ("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
        ("\u2028", "\\u2028"), ("\u2029", "\\u2029"),
    ):
        payload = payload.replace(source, escaped)
    replacements = {
        "{{GENERATED_AT}}": html.escape(data["generated_at"]),
        "{{DATA_JSON}}": payload,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            stream.write(template)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = Path(stream.name)
        os.replace(temporary_path, output)
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(generate_report(args.db, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
