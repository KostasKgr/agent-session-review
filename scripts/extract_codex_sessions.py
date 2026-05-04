#!/usr/bin/env python3
"""Extract codex session transcripts for a project folder.

Walks ~/.codex/sessions/ for rollout-*.jsonl files, filters by the session's
recorded cwd, and writes per-session transcripts plus a consolidated
chronological log of user prompts.

Designed to be run by the codex-session-review skill. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

IDE_PROMPT_MARKER = re.compile(r"## My request for Codex:\s*(.*)", re.DOTALL)


def iter_session_files(sessions_root: Path) -> Iterable[Path]:
    yield from sessions_root.rglob("rollout-*.jsonl")


def read_session_meta(path: Path) -> dict | None:
    try:
        with path.open() as f:
            first = f.readline()
        if not first.strip():
            return None
        obj = json.loads(first)
        if obj.get("type") != "session_meta":
            return None
        return obj.get("payload") or {}
    except (OSError, json.JSONDecodeError):
        return None


def unwrap_user_prompt(msg: str) -> str | None:
    """Return the real user prompt, or None if this message is harness noise."""
    if not msg:
        return None
    m = IDE_PROMPT_MARKER.search(msg)
    if m:
        return m.group(1).strip() or None
    # Skip system-injected messages that aren't user prompts.
    if msg.startswith(("<subagent_notification>", "<permissions", "<environment_context")):
        return None
    return msg.strip() or None


def extract_messages(path: Path) -> list[tuple[str, str, str]]:
    """Return chronological (timestamp, role, message) tuples."""
    out: list[tuple[str, str, str]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "event_msg":
            continue
        payload = rec.get("payload") or {}
        ts = rec.get("timestamp", "")
        if payload.get("type") == "user_message":
            prompt = unwrap_user_prompt(payload.get("message", ""))
            if prompt:
                out.append((ts, "USER", prompt))
        elif payload.get("type") == "agent_message":
            msg = payload.get("message", "")
            if msg:
                out.append((ts, "AGENT", msg))
    return out


def resolve_project(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"--since must be YYYY-MM-DD, got: {s}")


def session_started_at(meta: dict) -> datetime | None:
    ts = meta.get("timestamp") or ""
    try:
        # Tolerate trailing Z.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.getcwd(),
                    help="Project folder to filter sessions by (default: cwd)")
    ap.add_argument("--out-dir", default=None,
                    help="Where to write transcripts. Defaults to /tmp/codex_session_review/<project-name>/")
    ap.add_argument("--sessions-root", default=str(Path.home() / ".codex" / "sessions"),
                    help="Codex sessions root (default: ~/.codex/sessions)")
    ap.add_argument("--since", default=None,
                    help="Only include sessions started on/after this date (YYYY-MM-DD)")
    ap.add_argument("--json-summary", action="store_true",
                    help="Print a JSON summary to stdout instead of human-readable text")
    args = ap.parse_args()

    project = resolve_project(args.project)
    sessions_root = Path(args.sessions_root).expanduser()
    since = parse_date(args.since)

    if not sessions_root.exists():
        print(f"error: sessions root not found: {sessions_root}", file=sys.stderr)
        return 2

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        safe = Path(project).name or "root"
        out_dir = Path("/tmp/codex_session_review") / safe
    out_dir.mkdir(parents=True, exist_ok=True)

    matched: list[tuple[Path, dict]] = []
    for p in iter_session_files(sessions_root):
        meta = read_session_meta(p)
        if not meta:
            continue
        if meta.get("cwd") != project:
            continue
        if since is not None:
            started = session_started_at(meta)
            if started is None or started.replace(tzinfo=None) < since:
                continue
        matched.append((p, meta))

    matched.sort(key=lambda x: x[0].name)

    all_prompts: list[tuple[str, str, str]] = []  # (timestamp, session_name, prompt)
    per_session_stats: list[dict] = []

    for path, meta in matched:
        msgs = extract_messages(path)
        transcript_path = out_dir / (path.stem + ".txt")
        with transcript_path.open("w") as w:
            w.write(f"# session: {path.name}\n")
            w.write(f"# cwd:     {meta.get('cwd','')}\n")
            w.write(f"# started: {meta.get('timestamp','')}\n")
            git = meta.get("git") or {}
            if git:
                w.write(f"# branch:  {git.get('branch','')}  commit: {git.get('commit_hash','')}\n")
            w.write("\n")
            for ts, role, m in msgs:
                w.write(f"=== [{ts}] {role} ===\n{m}\n\n")

        user_msgs = [(ts, m) for ts, role, m in msgs if role == "USER"]
        per_session_stats.append({
            "session": path.name,
            "started": meta.get("timestamp", ""),
            "user_prompts": len(user_msgs),
            "agent_messages": sum(1 for _, r, _ in msgs if r == "AGENT"),
            "transcript": str(transcript_path),
        })
        for ts, m in user_msgs:
            all_prompts.append((ts, path.name, m))

    consolidated = out_dir / "ALL_USER_PROMPTS.txt"
    all_prompts.sort(key=lambda x: x[0])
    with consolidated.open("w") as w:
        w.write(f"# project: {project}\n")
        w.write(f"# sessions: {len(matched)}\n")
        w.write(f"# prompts:  {len(all_prompts)}\n\n")
        for ts, fn, m in all_prompts:
            w.write(f"=== [{ts}] {fn} ===\n{m}\n\n")

    summary = {
        "project": project,
        "sessions_root": str(sessions_root),
        "out_dir": str(out_dir),
        "consolidated_prompts": str(consolidated),
        "session_count": len(matched),
        "total_user_prompts": len(all_prompts),
        "since": args.since,
        "sessions": per_session_stats,
    }

    if args.json_summary:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"project:    {project}")
    print(f"sessions:   {len(matched)} matched (of all under {sessions_root})")
    print(f"prompts:    {len(all_prompts)} user prompts")
    print(f"out dir:    {out_dir}")
    print(f"all prompts: {consolidated}")
    if matched:
        print()
        print("per-session (oldest first):")
        for s in sorted(per_session_stats, key=lambda s: s["started"]):
            print(f"  {s['started']}  {s['user_prompts']:>3} prompts  {s['session']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
