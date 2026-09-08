"""Generate a self-contained local dashboard from Skill invocation data."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable

import scanner


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "analytics.db"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "dashboard.html"
DEFAULT_TEMPLATE_PATH = PROJECT_ROOT / "templates" / "dashboard.html"
_FRONTMATTER_NAME = re.compile(r"^name\s*:\s*(.+?)\s*$", re.IGNORECASE)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _skill_name(path: Path) -> str:
    fallback = path.parent.name or "unknown"
    try:
        with path.open("r", encoding="utf-8") as stream:
            if stream.readline().strip() != "---":
                return fallback
            for line_number, line in enumerate(stream, start=2):
                if line.strip() == "---" or line_number > 80:
                    break
                match = _FRONTMATTER_NAME.match(line)
                if match:
                    return match.group(1).strip().strip("\"'") or fallback
    except (OSError, UnicodeError):
        pass
    return fallback


def _version_key(path: Path) -> tuple[tuple[int, int | str], ...]:
    version = path.name
    manifest = path / ".codex-plugin" / "plugin.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("version"):
            version = str(value["version"])
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.findall(r"\d+|[^\d]+", version)
    )


def _plugin_skill_paths(plugin_root: Path) -> Iterable[Path]:
    if not plugin_root.is_dir():
        return
    for marketplace in sorted(path for path in plugin_root.iterdir() if path.is_dir()):
        for plugin in sorted(path for path in marketplace.iterdir() if path.is_dir()):
            if plugin.name.casefold().startswith("plugin-backup-"):
                continue
            latest = plugin / "latest"
            if latest.is_dir():
                current = latest.resolve()
            else:
                versions = [path for path in plugin.iterdir() if path.is_dir()]
                if not versions:
                    continue
                current = max(versions, key=_version_key)
            yield from current.rglob("SKILL.md")


def discover_skills(
    codex_home: str | os.PathLike[str] | None = None,
    agents_home: str | os.PathLike[str] | None = None,
    plugin_root: str | os.PathLike[str] | None = None,
) -> list[dict[str, str]]:
    """Enumerate currently installed Skill instruction files by canonical path."""
    codex = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    agents = Path(agents_home or Path.home() / ".agents")
    plugins = Path(plugin_root or codex / "plugins" / "cache")
    codex_skills = codex / "skills"
    system_skills = codex_skills / ".system"

    roots: tuple[tuple[Iterable[Path], str], ...] = (
        (codex_skills.rglob("SKILL.md") if codex_skills.is_dir() else (), "codex"),
        ((agents / "skills").rglob("SKILL.md") if (agents / "skills").is_dir() else (), "agents"),
        (_plugin_skill_paths(plugins), "plugin"),
    )
    discovered: dict[str, dict[str, str]] = {}
    for paths, root_source in roots:
        for path in sorted(paths):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            name = _skill_name(resolved)
            identity = name.casefold()
            if identity in discovered:
                continue
            source = (
                "system"
                if root_source == "codex" and system_skills in resolved.parents
                else "other"
                if root_source == "codex"
                else root_source
            )
            discovered[identity] = {
                "path": os.path.normcase(str(resolved)),
                "name": name,
                "source": source,
            }
    return sorted(discovered.values(), key=lambda item: (item["name"].casefold(), item["path"]))
def _refresh_skills(
    connection: sqlite3.Connection,
    installed: list[dict[str, str]],
    seen_at: str,
) -> None:
    for skill in installed:
        connection.execute(
            """
            INSERT INTO skills
                (skill_path, skill_name, skill_source, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(skill_path) DO UPDATE SET
                skill_name = excluded.skill_name,
                skill_source = excluded.skill_source,
                last_seen_at = excluded.last_seen_at
            """,
            (skill["path"], skill["name"], skill["source"], seen_at, seen_at),
        )


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
    """Refresh installed Skills and return presentation-ready aggregate data."""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.replace(tzinfo=local_timezone or timezone.utc)
    installed = discover_skills(codex_home, agents_home, plugin_root)
    connection = scanner.init_db(db_path)
    connection.row_factory = sqlite3.Row
    try:
        _refresh_skills(connection, installed, _iso_utc(current))
        connection.commit()
        invocation_rows = connection.execute(
            """
            SELECT i.skill_path, i.invoked_at, i.cwd, i.agent_kind,
                   s.skill_name, s.skill_source
            FROM invocations AS i
            JOIN skills AS s ON s.skill_path = i.skill_path
            ORDER BY i.invoked_at
            """
        ).fetchall()
        diagnostic_counts = dict(
            connection.execute(
                "SELECT category, COUNT(*) FROM diagnostics GROUP BY category"
            ).fetchall()
        )
    finally:
        connection.close()

    installed_by_name = {skill["name"].casefold(): skill for skill in installed}
    daily_counts: Counter[str] = Counter()
    weekly_counts: Counter[str] = Counter()
    monthly_counts: Counter[str] = Counter()
    monthly_skills: dict[str, set[str]] = defaultdict(set)
    agent_counts: Counter[str] = Counter()
    cwd_counts: Counter[str] = Counter()
    per_skill: dict[str, dict[str, Any]] = {}
    weekly_detail: Counter[tuple[str, str, str]] = Counter()

    for row in invocation_rows:
        local = _local_datetime(row["invoked_at"], local_timezone)
        if local is None:
            continue
        day = local.date().isoformat()
        week = (local.date() - timedelta(days=local.weekday())).isoformat()
        month = day[:7]
        identity = row["skill_name"].casefold()
        canonical = installed_by_name.get(identity)
        skill_path = canonical["path"] if canonical else row["skill_path"]
        skill_name = canonical["name"] if canonical else row["skill_name"]
        skill_source = canonical["source"] if canonical else row["skill_source"]
        agent = row["agent_kind"]
        daily_counts[day] += 1
        weekly_counts[week] += 1
        monthly_counts[month] += 1
        monthly_skills[month].add(identity)
        agent_counts[agent] += 1
        cwd_counts[row["cwd"] or "未知目录"] += 1
        weekly_detail[(week, skill_path, agent)] += 1

        summary = per_skill.setdefault(
            identity,
            {
                "path": skill_path,
                "name": skill_name,
                "source": skill_source,
                "calls": 0,
                "days": set(),
                "last_invoked_at": row["invoked_at"],
            },
        )
        summary["calls"] += 1
        summary["days"].add(day)
        if (_parse_timestamp(row["invoked_at"]) or datetime.min.replace(tzinfo=timezone.utc)) > (
            _parse_timestamp(summary["last_invoked_at"]) or datetime.min.replace(tzinfo=timezone.utc)
        ):
            summary["last_invoked_at"] = row["invoked_at"]

    ranking = []
    for item in per_skill.values():
        ranking.append(
            {
                "path": item["path"],
                "name": item["name"],
                "source": item["source"],
                "calls": item["calls"],
                "active_days": len(item["days"]),
                "last_invoked_at": item["last_invoked_at"],
                "installed": item["name"].casefold() in installed_by_name,
            }
        )
    ranking.sort(key=lambda item: (-item["calls"], item["name"].casefold()))

    never_used = [skill for skill in installed if skill["name"].casefold() not in per_skill]
    cutoff = current.astimezone(local_timezone).date() - timedelta(days=30) if local_timezone else current.astimezone().date() - timedelta(days=30)
    stale = [
        item
        for item in ranking
        if item["installed"]
        and (_local_datetime(item["last_invoked_at"], local_timezone) is not None)
        and _local_datetime(item["last_invoked_at"], local_timezone).date() < cutoff
    ]

    today = current.astimezone(local_timezone) if local_timezone else current.astimezone()
    start_day = today.date() - timedelta(days=364)
    daily = [
        {"date": (start_day + timedelta(days=offset)).isoformat(), "count": daily_counts[(start_day + timedelta(days=offset)).isoformat()]}
        for offset in range(365)
    ]
    current_week = today.date() - timedelta(days=today.weekday())
    week_keys = [(current_week - timedelta(weeks=offset)).isoformat() for offset in range(11, -1, -1)]
    weekly = [{"week": key, "count": weekly_counts[key]} for key in week_keys]
    month_keys = _month_keys(today)
    monthly = []
    for index, key in enumerate(month_keys):
        count = monthly_counts[key]
        previous = monthly_counts[month_keys[index - 1]] if index else 0
        change = round((count - previous) * 100 / previous, 1) if previous else (0.0 if count == 0 else None)
        monthly.append(
            {"month": key, "count": count, "active_skills": len(monthly_skills[key]), "change_percent": change}
        )

    detail = [
        {"week": week, "skill_path": path, "agent": agent, "count": count}
        for (week, path, agent), count in sorted(weekly_detail.items())
        if week in week_keys
    ]
    active_days = len({day for item in per_skill.values() for day in item["days"]})
    return {
        "generated_at": _iso_utc(current),
        "overview": {
            "total_calls": len(invocation_rows),
            "installed_skills": len(installed),
            "used_skills": len(per_skill),
            "never_used_skills": len(never_used),
            "active_days": active_days,
        },
        "daily": daily,
        "weekly": weekly,
        "weekly_detail": detail,
        "monthly": monthly,
        "agent_counts": {kind: agent_counts[kind] for kind in ("main", "subagent", "unknown")},
        "cwd_counts": [
            {"cwd": cwd, "count": count}
            for cwd, count in sorted(cwd_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "ranking": ranking,
        "never_used": never_used,
        "stale_30_days": stale,
        "quality": {
            "unparsed_events": diagnostic_counts.get("invalid_json", 0)
            + diagnostic_counts.get("invalid_encoding", 0),
            "unknown_agents": diagnostic_counts.get("unknown_agent", 0),
            "scan_failures": diagnostic_counts.get("scan_failure", 0),
        },
    }


def _overview_markup(data: dict[str, Any]) -> str:
    cards = (
        ("总调用", data["total_calls"]),
        ("已使用 Skill", data["used_skills"]),
        ("从未使用", data["never_used_skills"]),
        ("活跃天数", data["active_days"]),
    )
    return "".join(
        f'<article class="metric"><span>{html.escape(label)}</span><strong>{value}</strong></article>'
        for label, value in cards
    )


def _ranking_markup(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="empty">还没有可观测的 Skill 调用</td></tr>'
    return "".join(
        "<tr>"
        f'<td><strong>{html.escape(row["name"])}</strong><small>{html.escape(row["source"])}</small></td>'
        f'<td class="number">{row["calls"]}</td>'
        f'<td class="number">{row["active_days"]}</td>'
        f'<td>{html.escape(row["last_invoked_at"])}</td>'
        f'<td><code title="{html.escape(row["path"], quote=True)}">{html.escape(row["path"])}</code></td>'
        "</tr>"
        for row in rows
    )


def _skill_list_markup(rows: list[dict[str, Any]], empty_text: str) -> str:
    if not rows:
        return f'<li class="empty">{html.escape(empty_text)}</li>'
    return "".join(
        f'<li><span>{html.escape(row["name"])}</span><small>{html.escape(row["source"])}</small></li>'
        for row in rows
    )


def _quality_markup(data: dict[str, int]) -> str:
    labels = (
        ("无法解析事件", "unparsed_events"),
        ("未知代理类型", "unknown_agents"),
        ("扫描失败", "scan_failures"),
    )
    return "".join(
        f'<article><strong>{data[key]}</strong><span>{label}</span></article>' for label, key in labels
    )


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
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    replacements = {
        "{{GENERATED_AT}}": html.escape(data["generated_at"]),
        "{{OVERVIEW_CARDS}}": _overview_markup(data["overview"]),
        "{{RANKING_ROWS}}": _ranking_markup(data["ranking"]),
        "{{NEVER_ROWS}}": _skill_list_markup(data["never_used"], "所有已安装 Skill 都有调用记录"),
        "{{STALE_ROWS}}": _skill_list_markup(data["stale_30_days"], "没有 30 天以上未调用的 Skill"),
        "{{QUALITY_CARDS}}": _quality_markup(data["quality"]),
        "{{DATA_JSON}}": payload,
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template, encoding="utf-8", newline="\n")
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
