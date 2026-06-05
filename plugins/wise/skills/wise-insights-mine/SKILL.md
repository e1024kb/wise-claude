---
name: wise-insights-mine
description: >-
  Mine your own Claude Code session history for recurring task patterns and,
  once a pattern recurs across enough distinct sessions, draft it into a
  reusable personal skill under ~/.claude/skills/ — after you approve each one.
  This is the wise self-improvement loop: a SessionEnd hook quietly records each
  finished session into a local ledger, and this command clusters those sessions
  by frequency and proposes the strongest recurring patterns as new skills.
  Fully local; nothing leaves your machine. Invoked as `/wise-insights-mine`
  (bare alias) or `/wise:wise-insights-mine` (canonical). Use when the user says
  "mine my sessions", "find recurring patterns", "suggest skills", "what should
  I turn into a skill", "self-improve", or types `/wise-insights-mine`.
argument-hint: "[--here] [--since <N>d] [--min-count <N>] [--include-automated]"
model: opus
allowed-tools: Read, Write, AskUserQuestion, Bash(${CLAUDE_PLUGIN_ROOT}/scripts/bootstrap-deps.sh:*), Bash(${CLAUDE_PLUGIN_ROOT}/scripts/insights.py:*), Bash(bash:*), Bash(python3:*)
---

# /wise-insights-mine — turn recurring sessions into skills

## Why this skill exists

Claude Code already records every session as JSONL under
`~/.claude/projects/<slug>/*.jsonl`. The wise SessionEnd hook
(`hooks/session-end-ingest.sh`) ingests each finished session into a local
insights ledger under `wise_data_root()/insights/`. This command reads that
ledger, clusters the genuine (typed) user prompts by a deterministic
recurring-vocabulary fingerprint, counts how many **distinct sessions** each
pattern appears in, and surfaces any pattern over a frequency threshold as a
**candidate skill**. You approve each candidate before anything is written.

The heavy lifting — parsing, redaction, clustering, frequency-gating,
suppression of already-decided patterns — lives in
`${CLAUDE_PLUGIN_ROOT}/scripts/insights.py` (stdlib-only). This skill's job is to
run it, present the gated candidates, and — on your approval — draft the skill
file and record the decision. It never invents the frequency counts; it relays
them.

## Arguments

Parse `$ARGUMENTS` as a flag string (any order; all optional):

- `--here` — only mine sessions whose working directory is the current repo.
  Default mines all projects.
- `--since <N>d` (or `<N>w`) — only consider sessions from the last N days/weeks.
  Bounds the first full backfill; omit to consider everything.
- `--min-count <N>` — frequency threshold: a pattern must recur across at least
  N distinct sessions to be a candidate. Default 3.
- `--include-automated` — also show machine-generated / headless patterns
  (recur byte-identically across many sessions). Hidden by default.

Validation: if `$ARGUMENTS` contains an unrecognised token, state which one and
stop — do not guess.

## Procedure

### 1. Run the miner

Forward the parsed flags straight to the engine and ask for JSON:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" mine --json [parsed flags]
```

`insights.py` needs only Python 3 (standard library — no pyyaml/ulid, so the
workflow init-check does **not** apply here). If the call fails because
`python3` is missing, tell the user to install Python 3 or run `/wise-init`, then
stop.

### 2. Read the result

Stdout is a JSON object with: `sessions_in_scope`, `newly_ingested`,
`suppressed_automated`, a full `candidates` array, and a `gated` array (the
pending candidates at/over threshold, already excluding dismissed/promoted
patterns and — unless `--include-automated` — machine-generated ones). Work from
`gated`.

If `gated` is empty: report `sessions_in_scope`, note how many automated
patterns were hidden (`suppressed_automated`) and that `--include-automated`
reveals them, suggest lowering `--min-count` or widening `--since`, and stop.

### 3. Offer each candidate (cap at the top 5 by session_count)

For each gated candidate, present an `AskUserQuestion` (one question per
candidate, or batch a few) with the candidate's evidence in the question text:

- the `label` (most common phrasing),
- `session_count` and `distinct_phrasings` — call out when
  `likely_automated` is true or `distinct_phrasings` is 1 ("looks
  machine-generated"),
- 2–3 `example_prompts`,
- the dominant tool sequence from `tool_sigs[0]`.

Options: **Draft skill** / **Dismiss permanently** / **Skip for now**.

### 4a. Draft

1. Propose a kebab-case skill name derived from the label (e.g. "Let's commit
   and push" → `commit-and-push`); let the user confirm or rename.
2. **Refuse** to overwrite an existing `~/.claude/skills/<name>/` — if it
   exists, ask for a different name.
3. `Write` `~/.claude/skills/<name>/SKILL.md` using the template below,
   populated from the candidate's evidence. The skill is description-triggered
   (no `argument-hint`) so it auto-activates on matching prose.
4. Record the decision so the pattern never resurfaces:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" mark <cluster_id> promoted \
     --skill-name <name> --skill-path "$HOME/.claude/skills/<name>/SKILL.md"
   ```

Drafted-skill template (fill the `<…>` slots; keep the provenance comment):

```markdown
---
name: <name>
description: >-
  <One sentence describing the recurring task, synthesised from the example
  prompts>. Use when the user says <2–4 trigger phrases drawn from the example
  prompts>.
---

<!-- DRAFT skill generated by /wise-insights-mine on <YYYY-MM-DD> from your local
     Claude Code session history (cluster <cluster_id>, seen in <session_count>
     sessions). Derived from redacted local transcripts — nothing left your
     machine. Review and refine the procedure before relying on it. -->

# <Title Case Of Name>

## When to use
<When the user is doing the recurring task — paraphrase the example prompts.>

## Procedure
<A numbered scaffold seeded from the observed tool sequence
 (<tool_sigs[0]>). Translate the tools into concrete steps, e.g. a Bash/git
 sequence, an MCP call, edits. Mark anything uncertain as TODO for the user.>
1. …
2. …

## Notes
- This was learned from <session_count> past sessions; the steps are a starting
  point, not a guarantee. Edit freely.
```

### 4b. Dismiss

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/insights.py" mark <cluster_id> dismissed
```

A dismissed pattern stays suppressed on future runs even if its frequency keeps
climbing.

### 4c. Skip

Do nothing — the candidate stays `pending` and will resurface next run.

### 5. Summary

Report what was drafted (with paths), dismissed, and skipped. Remind the user
that drafted skills are starting points to refine, and that they can re-run
`/wise-insights-mine` any time.

## Guardrails

- **Approval-gated.** Never write a skill without an explicit per-candidate
  `AskUserQuestion` confirmation. The `insights.py` engine never writes skills;
  only this skill's `Write` does.
- **Write only under `~/.claude/skills/`.** Never write a drafted skill anywhere
  else — not the wise plugin tree, not the current project. Never overwrite an
  existing skill directory.
- **Relay, don't fabricate.** The frequency counts, example prompts, and tool
  sequences come from `insights.py` output. Do not invent patterns or inflate
  counts.
- **Privacy.** Example prompts are already redacted by the engine; still, do not
  echo anything that looks like a secret. The ledger lives locally under
  `~/.local/share/wise/insights/`; mention `insights.py purge --yes` if the user
  wants to wipe it.
- **Do not invoke other wise action skills.** This skill stands alone. Drafting
  is done from the inline template, not by delegating to `skill-creator`.
- **No background work, no subagents.**
