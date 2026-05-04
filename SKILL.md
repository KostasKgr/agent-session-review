---
name: codex-session-review
description: Use when the user asks to review, summarize, gather insights, or estimate time spent from prior agent-session history for a project folder either Codex (~/.codex/sessions) or Claude Code (~/.claude/projects). Two modes content review (pivots, corrections, scope decisions, workflow patterns) and time-spent analysis (active hours per day from merged event timestamps). Typical prompts "review my codex session history for this project", "where did I steer the model differently", "how many hours did I spend working with the AI on this project".
---

# Session History Review

Two modes:

1. **Content review** extract transcripts for the project, analyze how the user steered the agent (pivots, corrections, scope-narrowing, regressions caught, teaching moments, workflow patterns).
2. **Time-spent analysis** merge event timestamps from codex + claude + git commits into per-day working blocks, subtracting breaks, to estimate active hours.

Pick the mode from the user's intent; don't run both unless asked.

## Scripts

Three scripts live in `scripts/`:

- `extract_codex_sessions.py` parses `~/.codex/sessions/**/rollout-*.jsonl`, writes transcripts
- `extract_claude_sessions.py` parses `~/.claude/projects/<encoded-cwd>/*.jsonl`, writes transcripts
- `activity_timeline.py` merges codex + claude + git timestamps, computes per-day active hours

The two extractors produce the same output shape under a per-project output directory:

- `ALL_USER_PROMPTS.txt` every user prompt, chronologically ordered across sessions
- one `.txt` per session full USER/AGENT transcript with timestamps
- a summary (stdout, or `--json-summary` for machine-readable)

`activity_timeline.py` does not write files; it prints a per-day table to stdout (or `--json`).

# Mode 1 Content Review

### Step 1 run the right extractor(s)

Default target is the current working directory. Override with `--project`.

```bash
# Codex history for the current project
~/.claude/skills/codex-session-review/scripts/extract_codex_sessions.py

# Claude Code history for a specific project
~/.claude/skills/codex-session-review/scripts/extract_claude_sessions.py --project /path/to/project

# Optional: only sessions since a date
~/.claude/skills/codex-session-review/scripts/extract_codex_sessions.py --since 2026-03-01
```

If the user didn't say which agent, ask briefly "codex, claude code, or both?" and only run what's needed. Don't run both by default; it doubles the work.

### Step 2 read the consolidated prompt log

`ALL_USER_PROMPTS.txt` is usually enough for the review. It has all user turns in chronological order with per-session file markers, so you can see the arc of the work without loading every transcript.

Read individual session `.txt` files only when:
- a specific prompt needs surrounding context (what did the agent produce that the user then corrected?)
- you want to quote a specific agent reply

### Step 3 write the review around specific signal categories

Don't produce a generic "here's what happened" summary. Look for these categories and organize the review by them. Skip categories that have no examples, don't pad.

- **Design pivots** user rejected the agent's proposed approach and chose a different direction. Evidence: ...
- **Overrides and corrections** user told the agent to undo or change something after seeing output. 
- **Scope narrowing** explicit constraints on blast radius. These reveal the user's judgment about where the agent was overreaching.
- **Regressions caught** user spotted that the agent broke something. These are the clearest evidence of the user keeping the agent honest.
- **Hygiene catches** smaller corrections that don't block progress but show the user checking the agent's work: missing gitignore entries, unwanted dependencies, stale TODOs etc
- **Teaching-mode detours** user asked for explanation alongside (or instead of) implementation. These reveal the user was learning in the loop, not just delegating.
- **Workflow patterns** how the user delegates across the corpus. Common observations:
   - Pre-written plans pasted in (`PLEASE IMPLEMENT THIS PLAN:`) used as delegation contracts
   - Pasting raw errors / command output instead of describing them
   - Asking for options/recommendations before telling the agent to implement

### Step 4 attribution rules

- Quote user language verbatim when it's short and characteristic. Paraphrase when it's long.
- Reference session files by name when useful: `rollout-2026-03-19T...jsonl` or `<session-id>.jsonl`.
- Include dates when the user is reasoning about an arc (e.g., "Day 1 was X, by day 3 it had shifted to Y").
- If you read an individual session transcript to find context, say so don't imply you read all of them if you didn't.

### Step 5 length discipline

- If there are fewer than ~20 user prompts, produce a tight single-section review, not a categorized one.
- If 20-100, use the categories above with 2-4 examples per category.
- If 100+, group by phase/date first (as an arc), then pull the strongest examples per category. Don't try to be comprehensive pick the most characteristic moments.

# Mode 2 Time-Spent Analysis

When the user asks "how many hours did I spend", "how long did this take", "how much time across days", or similar.

## Method

`activity_timeline.py` merges three timestamp streams for the target project:

1. **Codex events** `user_message` + `agent_message` event timestamps from sessions whose `cwd` matches the project.
2. **Claude Code events** real user turns + assistant turns from the encoded project dir (tool results and sidechain records filtered out).
3. **Git commits** author times (`%aI`) from `git log`.

Those are bucketed by local date. Within a day, any gap ≥ the break threshold (default 15 min) starts a new working block. Per-block duration is `last_ts - first_ts`; per-day active time is the sum. Single-event blocks contribute 0.

## Running the scripts

The scripts are located relative to the skill folder.

```bash
# Default merge all available sources, 15-min break threshold
scripts/activity_timeline.py --project /path/to/project

# Only codex + git (skip claude)
scripts/activity_timeline.py --sources codex+git

# Machine-readable for further processing
scripts/activity_timeline.py --json
```

## Interpreting And Reporting

**The automated number is a lower bound, not ground truth.** Always open the report with that framing. Event-driven measurement is blind to:

- **Quiet reading** docs, code review, thinking between turns longer than the break threshold
- **Pre-tooling work** days with a couple of manual commits and no agent activity (single-event blocks contribute 0)
- **Offline whiteboarding** sketching on paper, discussions, anything that doesn't touch git or an agent

### Producing the write-up

1. Run the script. Show the per-day table including the breaks list the breaks are evidence the user can validate against memory ("yes that lunch was really 80 minutes").
2. **Always flag days where the automated number is likely wrong.** Rules of thumb:
   - A day with fewer than ~5 events and a wall-span over 30 min → almost certainly undercounted; call it out and suggest the user add a manual note.
   - A day where the dominant work was research/doc-reading (look for wide gaps between codex events with no intermediate commits) → suggest the user's gut number may be higher.
3. **Ask for the user's own phase notes.** If they mention they kept notes, hours, or a rough phase breakdown, ask for them they're ground truth and the automated number should be reconciled against them, not the other way around.
4. When reconciling with user notes, present a side-by-side table: user phases / phase duration / automated / delta. Explain each non-trivial delta (usually: "X min of doc-reading that doesn't fire events"). Do NOT adjust the automated number to match; report both honestly.
5. Include a wall-time column alongside active time. The ratio (active/wall) is interesting a low ratio means the user took long breaks, a ratio near 1 means a focused single stretch.

### Choosing a break threshold

15 min is the default and a reasonable pick. Adjust by context:

- Raise to 20-25 min if the user does a lot of tight-loop pair programming and 15-min "breaks" would really be "reading a long codex reply". Look for many 15-20 min gaps in the table and ask if those were actual breaks.
- Lower to 10 min if the user is highly interactive and anything longer than that is probably actual step-away time.
- Never raise past 30 min at that point you're gluing real lunch breaks back in.

If you change the threshold, re-run and mention it in the write-up.

## Caveats To Surface In The Report

- **Single-event days** a day with one commit and nothing else shows 0 active minutes. Flag it explicitly if wall-span > a few minutes.
- **Sidechain / subagent work** not counted. If the user used subagents heavily, note that the real number is higher.
- **Multiple projects in one session** rare, but if codex or claude sessions touched the project mid-session while cwd was elsewhere, those won't be included.

# Appendix Notes On The Data Sources

- **Codex** records each CLI session as a single JSONL file. User messages arrive wrapped in an IDE-context preamble (`## My request for Codex:`); the script unwraps this.
- **Claude Code** records each session as one JSONL file per session ID under `~/.claude/projects/<encoded-cwd>/`, where the cwd is encoded by replacing both `/` and `.` with `-` (so `/home/alice/.local/share/foo` → `-home-alice--local-share-foo`).
- User messages in Claude Code arrive as `type:"user"` records with `message.content` containing text blocks; tool results also appear as `type:"user"` but have a `toolUseResult` field the scripts filter those out. IDE context and system-reminder blocks are stripped from prompt text.
- Subagent work in Claude Code lives under `<session-id>/subagents/` and is ignored by the default extractor and the timeline the main-thread transcript is what shows user intent.
- Worktree-based flows may write sessions under the worktree's path, not the canonical project path; those won't be found by `--project <canonical-path>`. If the user expects sessions that aren't showing up, check `~/.claude/projects/` directory listing for similarly-named encoded paths.
