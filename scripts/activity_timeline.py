#!/usr/bin/env python3
"""Estimate per-day active working time on a project.

Merges event timestamps from codex sessions, Claude Code sessions, and git
commits, buckets them by local date, splits each day into working blocks
separated by gaps >= break threshold, and sums block durations.

A block is `last_ts - first_ts`; single-event blocks contribute 0. This
deliberately undercounts quiet reading/thinking time that doesn't fire any
event — treat the output as a *lower bound*, not ground truth.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

# ---- source: codex ---------------------------------------------------------

def codex_events(sessions_root: Path, project: str) -> list[tuple[datetime, str]]:
    """Return (ts, source) tuples for user+agent messages across all matching codex sessions."""
    out: list[tuple[datetime, str]] = []
    if not sessions_root.exists():
        return out
    for p in sessions_root.rglob("rollout-*.jsonl"):
        try:
            with p.open() as f:
                first = f.readline()
            meta = json.loads(first)
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("type") != "session_meta":
            continue
        if (meta.get("payload") or {}).get("cwd") != project:
            continue
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "event_msg":
                continue
            pt = (d.get("payload") or {}).get("type")
            if pt not in ("user_message", "agent_message"):
                continue
            ts = d.get("timestamp")
            if not ts:
                continue
            try:
                out.append((datetime.fromisoformat(ts.replace("Z", "+00:00")), "codex"))
            except ValueError:
                continue
    return out


# ---- source: claude code ---------------------------------------------------

def encode_project_dir(project: str) -> str:
    return re.sub(r"[/.]", "-", project)


def claude_events(projects_root: Path, project: str) -> list[tuple[datetime, str]]:
    """Return (ts, source) tuples for real user turns + assistant messages."""
    out: list[tuple[datetime, str]] = []
    project_dir = projects_root / encode_project_dir(project)
    if not project_dir.is_dir():
        return out
    for p in project_dir.glob("*.jsonl"):
        if not p.is_file():
            continue
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = rec.get("type")
            if t not in ("user", "assistant"):
                continue
            if rec.get("isSidechain"):
                continue
            # Cross-check cwd when present; skip records not for this project.
            cwd = rec.get("cwd")
            if cwd and cwd != project:
                continue
            if t == "user":
                # Filter tool-result user records.
                if rec.get("toolUseResult") is not None:
                    continue
                msg = rec.get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    types = {c.get("type") for c in content if isinstance(c, dict)}
                    if types and types.issubset({"tool_result"}):
                        continue
            ts = rec.get("timestamp")
            if not ts:
                continue
            try:
                out.append((datetime.fromisoformat(ts.replace("Z", "+00:00")),
                            "claude-user" if t == "user" else "claude-agent"))
            except ValueError:
                continue
    return out


# ---- source: git -----------------------------------------------------------

def git_events(project: str) -> list[tuple[datetime, str]]:
    """Return author timestamps for all commits in the project."""
    try:
        out = subprocess.check_output(
            ["git", "-C", project, "log", "--pretty=format:%aI"],
            stderr=subprocess.DEVNULL,
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    events: list[tuple[datetime, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append((datetime.fromisoformat(line), "commit"))
        except ValueError:
            continue
    return events


# ---- analysis --------------------------------------------------------------

def split_blocks(times: list[datetime], break_min: int) -> tuple[list[list[int]], list[tuple[datetime, datetime, float]]]:
    """Return (blocks as index lists, breaks as (prev, next, minutes) tuples)."""
    if not times:
        return [], []
    blocks: list[list[int]] = []
    breaks: list[tuple[datetime, datetime, float]] = []
    cur = [0]
    for i in range(1, len(times)):
        gap_min = (times[i] - times[i - 1]).total_seconds() / 60
        if gap_min >= break_min:
            blocks.append(cur)
            breaks.append((times[i - 1], times[i], gap_min))
            cur = [i]
        else:
            cur.append(i)
    blocks.append(cur)
    return blocks, breaks


def fmt_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"--since must be YYYY-MM-DD, got: {s}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.getcwd(),
                    help="Project folder to analyze (default: cwd)")
    ap.add_argument("--sources", default="all", choices=["all", "codex", "claude", "git", "codex+git", "claude+git"],
                    help="Which signals to include (default: all available)")
    ap.add_argument("--break-min", type=int, default=15,
                    help="Gap in minutes that starts a new working block (default: 15)")
    ap.add_argument("--since", default=None,
                    help="Only include events on/after this date (YYYY-MM-DD)")
    ap.add_argument("--codex-root", default=str(Path.home() / ".codex" / "sessions"))
    ap.add_argument("--claude-root", default=str(Path.home() / ".claude" / "projects"))
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON instead of a human-readable table")
    args = ap.parse_args()

    project = str(Path(args.project).expanduser().resolve())
    since = parse_date(args.since)

    want_codex = args.sources in ("all", "codex", "codex+git")
    want_claude = args.sources in ("all", "claude", "claude+git")
    want_git = args.sources in ("all", "git", "codex+git", "claude+git")

    events: list[tuple[datetime, str]] = []
    if want_codex:
        events += codex_events(Path(args.codex_root).expanduser(), project)
    if want_claude:
        events += claude_events(Path(args.claude_root).expanduser(), project)
    if want_git:
        events += git_events(project)

    if since is not None:
        events = [
            (ts, src) for ts, src in events
            if ts.astimezone().replace(tzinfo=None) >= since
        ]

    if not events:
        print(f"no events found for {project} (sources: {args.sources})", file=sys.stderr)
        return 1

    # Bucket by local date.
    by_day: dict[str, list[tuple[datetime, str]]] = {}
    for ts, src in events:
        local = ts.astimezone()
        key = local.date().isoformat()
        by_day.setdefault(key, []).append((local, src))

    days_out: list[dict] = []
    total_active = timedelta()
    total_wall = timedelta()

    for date in sorted(by_day):
        pairs = sorted(by_day[date], key=lambda x: x[0])
        times = [t for t, _ in pairs]
        sources = [s for _, s in pairs]

        blocks, breaks = split_blocks(times, args.break_min)
        active = timedelta()
        for b in blocks:
            if len(b) >= 2:
                active += times[b[-1]] - times[b[0]]
        wall = times[-1] - times[0]
        total_active += active
        total_wall += wall

        src_counts: dict[str, int] = {}
        for s in sources:
            src_counts[s] = src_counts.get(s, 0) + 1

        days_out.append({
            "date": date,
            "first": times[0].strftime("%H:%M"),
            "last": times[-1].strftime("%H:%M"),
            "active_seconds": int(active.total_seconds()),
            "wall_seconds": int(wall.total_seconds()),
            "blocks": len(blocks),
            "events": len(times),
            "source_counts": src_counts,
            "breaks": [
                {
                    "from": a.strftime("%H:%M"),
                    "to": b.strftime("%H:%M"),
                    "minutes": round(m),
                }
                for a, b, m in breaks
            ],
        })

    result = {
        "project": project,
        "break_min": args.break_min,
        "sources_requested": args.sources,
        "total_active_seconds": int(total_active.total_seconds()),
        "total_wall_seconds": int(total_wall.total_seconds()),
        "days": days_out,
    }

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    # Human table
    print(f"project:    {project}")
    print(f"sources:    {args.sources}  (break threshold: {args.break_min} min)")
    print()
    header = f"{'date':<12} {'first':<6} {'last':<6} {'blocks':<6} {'active':<8} {'wall':<8} {'events':<6}  by source"
    print(header)
    print("-" * 110)
    for d in days_out:
        src = " ".join(f"{k}={v}" for k, v in sorted(d["source_counts"].items()))
        print(f"{d['date']:<12} {d['first']:<6} {d['last']:<6} {d['blocks']:<6} "
              f"{fmt_duration(timedelta(seconds=d['active_seconds'])):<8} "
              f"{fmt_duration(timedelta(seconds=d['wall_seconds'])):<8} "
              f"{d['events']:<6}  {src}")
        for br in d["breaks"]:
            print(f"             break  {br['from']} -> {br['to']}  ({br['minutes']}m)")
    print("-" * 110)
    print(f"{'total':<12} {'':<6} {'':<6} {'':<6} "
          f"{fmt_duration(total_active):<8} "
          f"{fmt_duration(total_wall):<8}")
    print()
    print("note: automated active time is a lower bound — it can't see doc-reading,")
    print("      whiteboarding, or any day without dense event activity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
