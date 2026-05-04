#!/usr/bin/env python3
"""Extract Claude Code session transcripts for a project folder.

Walks ~/.claude/projects/<encoded-cwd>/ for *.jsonl files, filters by the
session's recorded cwd, and writes per-session transcripts plus a
consolidated chronological log of user prompts.

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

# Tags wrapping IDE / harness context that shouldn't count as user intent.
WRAPPER_TAGS = [
    ("<ide_opened_file>", "</ide_opened_file>"),
    ("<ide_selection>", "</ide_selection>"),
    ("<system-reminder>", "</system-reminder>"),
    ("<local-command-stdout>", "</local-command-stdout>"),
    ("<command-name>", "</command-name>"),
    ("<command-message>", "</command-message>"),
    ("<command-args>", "</command-args>"),
    ("<bash-stdout>", "</bash-stdout>"),
    ("<bash-stderr>", "</bash-stderr>"),
]


def encode_project_dir(project: str) -> str:
    """Mirror Claude Code's project-dir encoding: replace / and . with -."""
    return re.sub(r"[/.]", "-", project)


def strip_wrappers(text: str) -> str:
    """Remove IDE/system-reminder/slash-command wrapper blocks from a user text."""
    out = text
    for open_tag, close_tag in WRAPPER_TAGS:
        pattern = re.compile(
            re.escape(open_tag) + r".*?" + re.escape(close_tag),
            re.DOTALL,
        )
        out = pattern.sub("", out)
    return out.strip()


def extract_user_prompt(content) -> str | None:
    """Pull a clean user prompt out of a message.content list."""
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            t = c.get("text", "")
            cleaned = strip_wrappers(t)
            if cleaned:
                parts.append(cleaned)
    if not parts:
        return None
    joined = "\n\n".join(parts).strip()
    return joined or None


def extract_assistant_text(content) -> str | None:
    if not isinstance(content, list):
        return None
    parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    joined = "\n\n".join(p for p in parts if p).strip()
    return joined or None


def is_real_user_turn(rec: dict) -> bool:
    """A human-authored user turn (not a tool result, not a sidechain)."""
    if rec.get("type") != "user":
        return False
    if rec.get("isSidechain"):
        return False
    if rec.get("toolUseResult") is not None:
        return False
    msg = rec.get("message") or {}
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, list):
        # Tool-result only records sometimes have no toolUseResult field at the top level
        # but the content is a single tool_result block.
        types = {c.get("type") for c in content if isinstance(c, dict)}
        if types and types.issubset({"tool_result"}):
            return False
    return True


def iter_session_files(project_dir: Path) -> Iterable[Path]:
    """Yield top-level <session-id>.jsonl files for a project dir."""
    if not project_dir.is_dir():
        return
    for p in sorted(project_dir.glob("*.jsonl")):
        if p.is_file():
            yield p


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"--since must be YYYY-MM-DD, got: {s}")


def session_start_time(path: Path) -> str:
    """Return the earliest timestamp in the file, or empty string."""
    try:
        for line in path.read_text().splitlines():
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d.get("timestamp")
            if ts:
                return ts
    except OSError:
        pass
    return ""


def extract_session(path: Path) -> dict:
    """Parse a session file; return per-session stats and transcript messages."""
    messages: list[tuple[str, str, str]] = []  # (ts, role_label, text)
    cwd = ""
    git_branch = ""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {"cwd": "", "messages": [], "git_branch": ""}

    for line in lines:
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not cwd and rec.get("cwd"):
            cwd = rec["cwd"]
        if not git_branch and rec.get("gitBranch"):
            git_branch = rec["gitBranch"]
        ts = rec.get("timestamp", "")
        t = rec.get("type")
        msg = rec.get("message") or {}

        if is_real_user_turn(rec):
            prompt = extract_user_prompt(msg.get("content"))
            if prompt:
                messages.append((ts, "USER", prompt))
        elif t == "assistant" and not rec.get("isSidechain"):
            text = extract_assistant_text(msg.get("content"))
            if text:
                messages.append((ts, "AGENT", text))

    return {"cwd": cwd, "messages": messages, "git_branch": git_branch}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.getcwd(),
                    help="Project folder to filter sessions by (default: cwd)")
    ap.add_argument("--out-dir", default=None,
                    help="Where to write transcripts. Defaults to /tmp/claude_session_review/<project-name>/")
    ap.add_argument("--projects-root", default=str(Path.home() / ".claude" / "projects"),
                    help="Claude Code projects root (default: ~/.claude/projects)")
    ap.add_argument("--since", default=None,
                    help="Only include sessions whose first timestamp is on/after this date (YYYY-MM-DD)")
    ap.add_argument("--json-summary", action="store_true",
                    help="Print a JSON summary to stdout instead of human-readable text")
    args = ap.parse_args()

    project = str(Path(args.project).expanduser().resolve())
    projects_root = Path(args.projects_root).expanduser()
    since = parse_date(args.since)

    if not projects_root.exists():
        print(f"error: projects root not found: {projects_root}", file=sys.stderr)
        return 2

    encoded = encode_project_dir(project)
    project_dir = projects_root / encoded
    if not project_dir.is_dir():
        print(f"error: no Claude sessions found for {project}", file=sys.stderr)
        print(f"       (expected: {project_dir})", file=sys.stderr)
        return 1

    if args.out_dir:
        out_dir = Path(args.out_dir).expanduser().resolve()
    else:
        safe = Path(project).name or "root"
        out_dir = Path("/tmp/claude_session_review") / safe
    out_dir.mkdir(parents=True, exist_ok=True)

    all_prompts: list[tuple[str, str, str]] = []  # (ts, session_file, prompt)
    per_session_stats: list[dict] = []

    for path in iter_session_files(project_dir):
        start_ts = session_start_time(path)
        if since is not None and start_ts:
            try:
                start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                if start_dt.replace(tzinfo=None) < since:
                    continue
            except ValueError:
                pass

        data = extract_session(path)
        # Skip sessions whose cwd doesn't match (e.g. worktree aliases).
        if data["cwd"] and data["cwd"] != project:
            continue

        transcript_path = out_dir / (path.stem + ".txt")
        with transcript_path.open("w") as w:
            w.write(f"# session: {path.name}\n")
            w.write(f"# cwd:     {data['cwd']}\n")
            w.write(f"# started: {start_ts}\n")
            if data["git_branch"]:
                w.write(f"# branch:  {data['git_branch']}\n")
            w.write("\n")
            for ts, role, m in data["messages"]:
                w.write(f"=== [{ts}] {role} ===\n{m}\n\n")

        user_msgs = [(ts, m) for ts, role, m in data["messages"] if role == "USER"]
        agent_msgs = [(ts, m) for ts, role, m in data["messages"] if role == "AGENT"]
        per_session_stats.append({
            "session": path.name,
            "started": start_ts,
            "user_prompts": len(user_msgs),
            "agent_messages": len(agent_msgs),
            "transcript": str(transcript_path),
        })
        for ts, m in user_msgs:
            all_prompts.append((ts, path.name, m))

    all_prompts.sort(key=lambda x: x[0])
    consolidated = out_dir / "ALL_USER_PROMPTS.txt"
    with consolidated.open("w") as w:
        w.write(f"# project: {project}\n")
        w.write(f"# sessions: {len(per_session_stats)}\n")
        w.write(f"# prompts:  {len(all_prompts)}\n\n")
        for ts, fn, m in all_prompts:
            w.write(f"=== [{ts}] {fn} ===\n{m}\n\n")

    summary = {
        "project": project,
        "projects_root": str(projects_root),
        "project_dir": str(project_dir),
        "out_dir": str(out_dir),
        "consolidated_prompts": str(consolidated),
        "session_count": len(per_session_stats),
        "total_user_prompts": len(all_prompts),
        "since": args.since,
        "sessions": per_session_stats,
    }

    if args.json_summary:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"project:     {project}")
    print(f"project dir: {project_dir}")
    print(f"sessions:    {len(per_session_stats)} matched")
    print(f"prompts:     {len(all_prompts)} user prompts")
    print(f"out dir:     {out_dir}")
    print(f"all prompts: {consolidated}")
    if per_session_stats:
        print()
        print("per-session (oldest first):")
        for s in sorted(per_session_stats, key=lambda s: s["started"]):
            print(f"  {s['started']}  {s['user_prompts']:>3} prompts  {s['session']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
