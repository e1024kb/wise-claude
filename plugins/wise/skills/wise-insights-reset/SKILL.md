---
name: wise-insights-reset
description: >-
  Reversibly clean up the self-improvement loop, and roll it back. Snapshots then
  removes the auto-created (wise-managed) skills and/or the insights index
  (ledger, candidates, decisions) into a timestamped restore point — and can
  restore any restore point. Never touches hand-written skills, and never
  hard-deletes (that's `insights.py purge`). Invoked as `/wise-insights-reset`
  (bare alias) or `/wise:wise-insights-reset` (canonical). Use when the user says
  "reset insights", "clean up auto-created skills", "wipe learned skills", "remove
  the skills mine made", "roll back insights", "restore insights", "undo mine".
argument-hint: "[--skills] [--index] [--dry-run] [--restore <ts>]"
model: opus
allowed-tools: Read, AskUserQuestion, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/insights.py:*), Bash(bash:*), Bash(python3:*)
---

# /wise-insights-reset — reversible cleanup + rollback

## Why this skill exists

`/wise-insights-mine` and `/wise-insights-refine` produce two kinds of artifact:
**learned skills** under `~/.claude/skills/` and an **index** under
`~/.local/share/wise/insights/` (`ledger.jsonl`, `candidates.json`,
`decisions.json`). This command cleans them up **reversibly**: it snapshots
everything it removes into `insights/snapshots/<ts>/` first, so any reset can be
rolled back. The same command does the rollback.

The destructive work is done entirely by `${CLAUDE_PLUGIN_ROOT}/scripts/insights.py`
(`reset` / `restore`), which snapshots-then-removes and only ever touches
wise-**managed** skills — so this skill needs no `rm`/`Write` grant, and
hand-written skills are structurally safe. For an irreversible wipe (including
the snapshots themselves), that's the separate `insights.py purge --yes`, which
this skill never invokes.

## Arguments

Parse `$ARGUMENTS` (any order; all optional):

- `--skills` — reset only the auto-created skills (leave the index).
- `--index` — reset only the index (leave the skills).
- (neither) — reset **both** (the default).
- `--dry-run` — show exactly what would be snapshotted/removed; change nothing.
- `--restore <ts>` — roll back: restore the snapshot with this timestamp.

Reject any unrecognised token with a clear message and stop. `--restore` is
mutually exclusive with the reset flags.

## Procedure

### 1. Guard on `/wise-init`

Enforce the setup gate per
`${CLAUDE_PLUGIN_ROOT}/references/insights-init-guard.md`: run the
`init-registry.py check` and, unless it prints `INIT:ok`, tell the user to run
`/wise-init` and **STOP**.

### 2. Branch on intent

- `--restore <ts>` present → go to **§5 Restore**.
- otherwise → **§3 / §4 Reset**.

### 3. Show state (and, if bare, let the user choose)

Run both, read the JSON:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" list-skills --json
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" list-snapshots --json
```

Report the count of wise-managed skills and the existing restore points. If the
user invoked the command **bare** (no scope/restore flags), present an
`AskUserQuestion`: **Reset (snapshot + clear)** / **Restore a snapshot** /
**Cancel**. (Restore → §5; Cancel → stop.)

### 4. Reset (snapshot-first, then confirm)

1. Run a dry run to compute the exact plan:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" reset --dry-run [--skills|--index] --json
   ```
2. Show the user precisely what will happen: which managed skills are removed,
   which index files archived, and the restore-point path. Make clear
   hand-written skills are untouched and the action is reversible.
3. `AskUserQuestion`: **Proceed** / **Cancel**. Only on Proceed:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" reset [--skills|--index] --json
   ```
4. Report the restore point and the exact rollback command
   (`/wise-insights-reset --restore <ts>`).

### 5. Restore

1. Resolve the `<ts>` — from `--restore`, or have the user pick one from
   `list-snapshots` via `AskUserQuestion`.
2. **Warn** that restoring **overwrites the current index** and restores skills,
   **skipping any skill name that already exists** (those are reported, never
   clobbered). `AskUserQuestion`: **Restore** / **Cancel**.
3. On Restore:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" restore <ts> --json
   ```
4. Report what was restored and what was skipped.

### 6. Summary

State the outcome and the restore-point path. Remind the user that resets are
reversible from `~/.local/share/wise/insights/snapshots/`, and that the
irreversible "destroy everything including snapshots" path is the separate
`insights.py purge --yes` (which this command never runs).

## Guardrails

- **Only wise-managed skills are removed** — the engine refuses to touch
  unmarked (hand-written) skills. Never delete a skill any other way.
- **Reset is always snapshot-first** (reversible). Never call `purge` here.
- **Restore never clobbers a live skill** — existing names are skipped + reported.
- **All destructive work goes through `insights.py`** — no `rm`, no `Write`.
- **Approval-gated.** Never reset or restore without an explicit `AskUserQuestion`
  confirmation. `--dry-run` changes nothing.
- **No background work, no subagents.**
