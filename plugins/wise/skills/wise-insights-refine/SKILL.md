---
name: wise-insights-refine
description: >-
  Review your learned skills for overlap and consolidate them — merge several
  redundant or composable skills into one aggregated skill and retire the
  originals (reversibly), after your approval. The "garden" pass to
  /wise-insights-mine's "harvest": mine creates skills from recurring sessions,
  refine keeps the resulting library tidy. Operates only on wise-managed skills
  (those carrying the wise-insights marker); hand-written skills are never
  auto-deleted — only suggested. Fully local. Invoked as `/wise-insights-refine`
  (bare alias) or `/wise:wise-insights-refine` (canonical). Use when the user
  says "refine my skills", "consolidate skills", "merge overlapping skills",
  "clean up auto-created skills", "dedupe my skills", or types
  `/wise-insights-refine`.
argument-hint: "[--dry-run] [--min-jaccard <X>] [--include-external]"
model: opus
allowed-tools: Read, Write, AskUserQuestion, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/init-registry.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/insights.py:*), Bash(bash:*), Bash(python3:*)
---

# /wise-insights-refine — consolidate learned skills

## Why this skill exists

`/wise-insights-mine` drafts a new `~/.claude/skills/<name>/SKILL.md` each time a
recurring pattern crosses its threshold. Over time the library accumulates
skills that overlap or compose. This command finds those overlaps and, with your
per-group approval, **merges** redundant skills into one aggregated skill and
**retires** the originals — reversibly (every retired skill is backed up first).

The deterministic work — enumerating skills, parsing the wise-insights marker,
computing token overlap, and the destructive retire (backup → verify → delete) —
lives in `${CLAUDE_PLUGIN_ROOT}/scripts/insights.py` (stdlib-only). This skill
runs it, judges which overlaps are genuinely the same task, synthesises the
merged skill, and orchestrates the safe ordering. It never deletes files itself;
retiring goes through the engine, which refuses to touch a skill that lacks the
marker — so **hand-written skills are structurally protected**.

## Arguments

Parse `$ARGUMENTS` as a flag string (any order; all optional):

- `--dry-run` — show the consolidation plan (groups, proposed names, what would
  be retired and where it'd be backed up) and make NO writes or retires.
- `--min-jaccard <X>` — overlap threshold (default 0.6). Lower surfaces looser
  overlaps; raise for only near-duplicates.
- `--include-external` — also surface overlaps that involve hand-written
  (unmarked) skills. These are **suggestion-only** — the external skill is never
  retired; you're just told it overlaps.

Validation: if `$ARGUMENTS` contains an unrecognised token, say which one and
stop — do not guess.

## Procedure

### 1. Guard on `/wise-init`, then enumerate + find overlaps

**First**, enforce the setup gate per
`${CLAUDE_PLUGIN_ROOT}/references/insights-init-guard.md`: run the
`init-registry.py check` and, unless it prints `INIT:ok`, tell the user to run
`/wise-init` and **STOP** — do not enumerate, do not bootstrap anything yourself.

Only on `INIT:ok`, in one message run both:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" list-skills --json
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" overlap --json [parsed flags]
```

If `overlap` returns no edges, report how many skills were scanned and stop —
the library is already tidy.

### 2. Form candidate groups from the pairwise edges

`overlap` returns **pairwise** edges (`{a, b, jaccard, shared_tokens,
retire_eligible, suggestion_only}`), NOT pre-merged groups — deliberately, so you
judge whether a 3-way merge is real. Cluster edges that share a member into a
candidate group ONLY when the members are genuinely the same task (read their
SKILL.md bodies to confirm; shared tokens like `git, commit, push` are a hint,
not proof). Do not blindly transitively merge A–B–C if A and C aren't actually
redundant. Drop or down-rank `suggestion_only` edges — those involve a
hand-written skill and cannot be retired.

### 3. Offer each group (cap at the top ~5 by overlap)

For each candidate group, present an `AskUserQuestion` with the evidence in the
question text: the member names, their Jaccard, the shared tokens, and a
one-line gloss of what each does. Options: **Merge** / **Keep separate** /
**Skip**. If a group mixes managed + external skills, say explicitly that the
external one will be kept (only suggested), and only the managed ones can be
retired.

### 4a. Merge — in this exact safe order

1. **Propose a fresh kebab-case name** for the merged skill. It MUST differ from
   every source name and MUST NOT collide with an existing `~/.claude/skills/`
   dir. Let the user confirm or rename.
2. **Write the replacement FIRST** — `~/.claude/skills/<new-name>/SKILL.md`,
   synthesised by reading the N source skills: union their procedures (dedup
   overlapping steps, keep the union of triggers in the description),
   `user-invocable: false`, and the refine marker as the first body line:
   ```
   <!-- wise-insights: source=wise-insights-refine v=1 merged-from=<name1,name2> retired-clusters=<cid1,cid2> created=<YYYY-MM-DD> -->
   ```
   `retired-clusters` = the `cluster=` values from each source's marker (omit any
   source that has none, e.g. a previously-merged skill). `created` = today.
3. **Then retire each managed source**, passing the new skill so the pattern is
   recorded as covered (not rejected):
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" retire <source-skill-dir> --superseded-by <new-name>
   ```
4. **Partial-failure guardrail.** Collect each retire result. If any retire fails
   (non-zero / "refusing"), STOP and report exactly: which merged skill was
   written, which sources were retired (with backup paths), which FAILED and why,
   and which were not attempted — and that re-running will finish, or the user
   can restore from the printed backup path. Never leave a half-merge silent.

### 4b. Keep separate / Skip

Do nothing — leave the skills as they are. (There is no "dismiss" for overlaps;
re-running will surface them again until you merge or the overlap changes.)

### 5. Summary

Report each group's outcome: merged (new skill path + which sources retired +
their backup dirs), kept, skipped. Remind the user the merged skills are starting
points to refine, that retired skills are recoverable from
`~/.local/share/wise/insights/skill-backups/`, and that deleting a merged skill
lets `/wise-insights-mine` resurface its underlying patterns.

## Guardrails

- **Only wise-managed skills are ever retired.** The engine refuses to retire a
  skill without the marker; never try to delete an external skill another way.
- **Replacement before retire, always.** Write the merged `SKILL.md` and confirm
  it exists before retiring any source, so the behaviour is never absent.
- **Approval per group.** Never merge/retire without an explicit `AskUserQuestion`
  confirmation. `--dry-run` writes and retires nothing.
- **No `rm`, no other destructive shell.** Deletion happens only via
  `insights.py retire`, which backs up first.
- **Write only under `~/.claude/skills/`**, and never overwrite an existing skill
  directory — pick a fresh, non-colliding name.
- **Don't fabricate overlaps.** The edges and tokens come from `insights.py`;
  judge them, don't invent them.
- **No background work, no subagents.**
