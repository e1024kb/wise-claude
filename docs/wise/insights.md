# wise insights — the self-improvement loop

> **Claude Code only.** The insights loop depends on the SessionEnd hook and
> Claude Code's on-disk session transcripts, so it is not shipped to the
> Codex / Cursor / Hermes ports — see the
> [compatibility matrix](../compatibility.md).

The **insights** subsystem lets `wise` learn from how you actually use Claude
Code. Claude Code already records every session as JSONL on disk; insights mines
that history for **recurring task patterns**, and — once a pattern recurs across
enough distinct sessions — drafts it into a reusable skill. Two more commands
keep the resulting skill library tidy and reversible.

It is **fully local**: nothing is sent anywhere, no account, no cloud. The whole
engine (`scripts/insights.py`) is Python-standard-library only.

The loop has three passes:

- **Harvest** — `/wise-insights-mine` turns recurring sessions into new skills.
- **Garden** — `/wise-insights-refine` merges overlapping skills and retires the
  originals.
- **Cleanup** — `/wise-insights-reset` reversibly removes the learned skills
  and/or the index, and rolls any reset back.

## The commands

| Invocation | Purpose |
|---|---|
| `/wise-insights-mine [--here] [--since <N>d] [--min-count <N>] [--include-automated]` | Mine session history for recurring patterns; draft the strongest into skills. |
| `/wise-insights-refine [--dry-run] [--min-jaccard <X>] [--include-external]` | Find overlapping learned skills; merge + retire (reversibly). |
| `/wise-insights-reset [--skills] [--index] [--dry-run] [--restore <ts>]` | Snapshot then clear the learned skills and/or the index; restore any snapshot. |

All three are **gated on `/wise-init`** — see [The `/wise-init` gate](#the-wise-init-gate).

## How the loop works

```
   every session ends                  you run, on demand
┌────────────────────────┐        ┌──────────────────────────────┐
│  SessionEnd hook        │        │  /wise-insights-mine          │
│  session-end-ingest.sh  │  ───▶  │  cluster + frequency-gate     │
└───────────┬─────────────┘        └───────────────┬──────────────┘
            │ ingest ONE transcript                │ draft (approve each)
            ▼                                       ▼
   insights/ledger.jsonl                  ~/.claude/skills/<name>/  ◀──┐
                                                    │                  │ merge overlaps
                          /wise-insights-refine ────┘ retire (backed up)│
                                                    │                  │
                          /wise-insights-reset ─────┴── snapshot ⇄ restore
```

1. **Capture** is automatic and invisible. When a session ends, the SessionEnd
   hook hands that one transcript to `insights.py ingest`, which appends a
   compact, **redacted** record to the ledger. No LLM, no network, never blocks
   exit.
2. **Mine** (manual) clusters the genuine prompts by a deterministic fingerprint,
   counts distinct sessions per pattern, and surfaces any pattern over the
   frequency threshold as a candidate for you to approve.
3. **Refine** (manual) compares the resulting skills and consolidates overlaps.
4. **Reset** (manual) cleans up — reversibly.

## Where things live

All persistent state is off the project tree, under the wise data root
(`~/.local/share/wise/`, honouring `XDG_DATA_HOME`; resolved via
`wise_data_root()` in `scripts/workflows.py`):

```
~/.local/share/wise/insights/
├── ledger.jsonl                 # one redacted record per ingested session
├── candidates.json              # derived, frequency-ranked patterns (rewritten by mine)
├── decisions.json               # promote / dismiss / retire suppression list
├── skill-backups/<ts>/<name>/   # per-skill backups from /wise-insights-refine retire
└── snapshots/<ts>/              # /wise-insights-reset restore points
    ├── index/                   # archived ledger.jsonl + candidates.json + decisions.json
    └── skills/<name>/           # archived skill dirs
```

Learned skills themselves live in the **user-global skills dir**,
`~/.claude/skills/<name>/SKILL.md` (override with `WISE_SKILLS_DIR` — see
[Testing](#testing--sandboxing)).

### The ledger record

One JSON object per session (keyed by `session_id`; re-ingest replaces it):

```json
{
  "session_id": "…", "transcript_path": "…", "cwd": "…", "git_branch": "main",
  "first_ts": "…", "last_ts": "…", "transcript_mtime": 0.0, "ingested_at": "…",
  "prompts": ["redacted, truncated user prompts"],
  "tool_sig": ["Bash", "Read", "Edit"],   // run-length-deduped tool NAMES only
  "prompt_count": 7
}
```

Tool **inputs are never stored** — only the sequence of tool names. Prompt text
is redacted before it lands here (see [Privacy](#privacy-model)).

## `/wise-insights-mine` — harvest

### Capture (the SessionEnd hook)

`hooks/session-end-ingest.sh` is the one sanctioned hook in the plugin (see
`CONTRIBUTING.md` §2.4). On session end it reads `transcript_path` from the
hook's stdin payload and runs `insights.py ingest <path>`. It is deliberately
tiny: stdlib-only (works before `/wise-init`), no LLM, no network, idempotent
(keyed on `session_id` + transcript mtime), and always exits 0 so it can never
block session teardown.

Because the hook only registers after the plugin is installed, the first `mine`
**self-heals** by back-filling from transcripts already on disk — you don't have
to wait for new sessions.

### Genuine-prompt filtering

A transcript line counts as a real typed request only when it is a `type:"user"`
event that is not `isMeta` / `isSidechain`, whose content is a plain string in
the **4–1200 char** band, and which is not one of: a slash-command echo, a
`<bash-input>`/`<bash-stdout>` bash-mode line, a `<task-notification>` /
`<system-reminder>` injection, a compaction-continuation preamble, or an
automation persona prompt (`You are …`, `Your task is …`, etc.). Up to 50
prompts per session are kept.

### Clustering + the frequency gate

Each kept prompt is normalised into a recurring-vocabulary fingerprint
(lowercased, code/URLs/paths/digits stripped, stopwords and <3-char tokens
dropped, light singular stemming). Prompts are grouped by a **stable cluster
key**: the top shared tokens (those with corpus document-frequency ≥ 2), capped
to 5, hashed to a 12-char `cluster_id`. Prompts with fewer than 2 content tokens
(one-word chatter) are ignored.

A cluster becomes a **candidate** when it appears in **≥ `--min-count` distinct
sessions** (default 3) and isn't already in `decisions.json`.

### Hiding machine-generated prompts

A pattern that recurs **byte-identically** (`distinct_phrasings == 1`) across
**≥ 4 sessions** is treated as machine/headless automation (e.g. a tool that
calls Claude in a loop) and hidden by default. `--include-automated` shows them;
the run always logs how many were suppressed (no silent caps).

### Flags

| Flag | Effect |
|---|---|
| `--here` | Only sessions whose `cwd` is the current repo. |
| `--since <N>d` (or `<N>w`) | Only the last N days/weeks; bounds the first full backfill. |
| `--min-count <N>` | Frequency threshold (default 3 distinct sessions). |
| `--include-automated` | Also surface byte-identical machine-generated patterns. |

### Drafting

For each gated candidate you choose **Draft / Dismiss / Skip**:

- **Draft** writes a starter `~/.claude/skills/<name>/SKILL.md` — description
  synthesised from the example prompts, a procedure scaffold seeded from the
  observed tool sequence, and the provenance marker (see [Marker](#the-provenance-marker)).
  It's recorded `promoted`.
- **Dismiss** suppresses the pattern forever (`dismissed`).
- **Skip** leaves it `pending` to resurface next run.

Drafted skills are written with **`user-invocable: false`**, so Claude
auto-invokes them when their description matches but they stay **out of your `/`
slash-command menu**. Nothing is written without explicit per-candidate approval.

## `/wise-insights-refine` — garden

Over time the library accumulates overlapping skills. Refine finds and
consolidates them.

### Overlap detection

`insights.py overlap` tokenises each skill's body — after stripping frontmatter,
the marker comment, markdown headings, and shared template boilerplate — and
computes **pairwise Jaccard** over a corpus-DF-filtered token set (tokens present
in *every* skill are dropped as non-discriminating). It emits **pairwise edges**
(`{a, b, jaccard, shared_tokens}`), not pre-merged groups, so the LLM/you confirm
whether a multi-way merge is genuinely one task. Default threshold
`--min-jaccard 0.6`. Edges that involve a hand-written (unmarked) skill are
**suggestion-only** and shown only with `--include-external`.

### Merge + retire

For each confirmed group you approve, refine:

1. **writes the merged replacement first** — a new `~/.claude/skills/<name>/`
   (fresh name, distinct from the sources) unioning their procedures, with a
   refine marker carrying `merged-from` and `retired-clusters`;
2. then **retires each managed source** via `insights.py retire`, which **refuses
   any skill without the marker** (hand-written skills are structurally safe),
   **backs the dir up** to `skill-backups/<ts>/` (verified) **before** removing
   it, and flags the source's cluster `retired` in `decisions.json`.

`retired` is distinct from `dismissed`: the pattern is *covered* by the merged
skill, not *rejected*. If you later delete the merged skill, `mine` reconciles —
it clears the orphaned `retired` decision so the pattern can resurface.

## `/wise-insights-reset` — reversible cleanup + rollback

Reset cleans up the loop **without losing the ability to undo it**. The key
principle: **reset = move-to-timestamped-snapshot, never hard-delete.**

`insights.py reset` snapshots everything in scope into `snapshots/<ts>/` (index
files → `index/`, managed skill dirs → `skills/`), verifies the copies, **then**
removes the live originals. Only wise-managed skills are touched; hand-written
skills are never in scope.

| Flag | Effect |
|---|---|
| (none) | Reset **both** the managed skills and the index. |
| `--skills` | Only the auto-created skills. |
| `--index` | Only the index (`ledger` / `candidates` / `decisions`). |
| `--dry-run` | Print the exact plan; change nothing. |
| `--restore <ts>` | Roll a restore point back. |

`restore` copies the snapshot's index back (overwriting the live index) and
restores skills, **skipping any name that already exists** (reported, never
clobbered). `list-snapshots` lists the restore points newest-first.

**Reset vs purge.** `/wise-insights-reset` is the everyday, reversible path.
`insights.py purge --yes` is the separate, **irreversible** wipe of the entire
store (snapshots and backups included) — the explicit escape hatch.

## The provenance marker

Every wise-generated skill carries a marker as its first body line. This is how
refine/reset tell **managed** skills (safe to merge/retire/reset) from
**hand-written** ones (never auto-touched) — by grepping
`~/.claude/skills/*/SKILL.md`. It is an HTML comment, not a frontmatter key
(Claude Code's handling of unknown frontmatter keys is undocumented):

```
<!-- wise-insights: source=wise-insights-mine v=1 cluster=<id> sessions=<N> created=<YYYY-MM-DD> -->
<!-- wise-insights: source=wise-insights-refine v=1 merged-from=<names> retired-clusters=<cids> created=<YYYY-MM-DD> -->
```

`v=1` versions the marker schema. The marker and the registry (`decisions.json`)
each independently identify a wise-made skill, so either alone is enough.

## Privacy model

- **Local only.** No network, no account. State lives under
  `~/.local/share/wise/insights/`.
- **Redaction at ingest.** Before any prompt text is stored, URLs, emails,
  filesystem paths, API-key-shaped secrets (`sk-…`, `ghp_…`, `AKIA…`, …), and
  long opaque tokens are replaced with placeholders; prompts are truncated to
  240 chars.
- **No tool inputs.** Only tool **names** are recorded, never their arguments
  (which is where file contents, commands, and secrets live).
- **Wipe it** any time with `insights.py purge --yes`, or reversibly with
  `/wise-insights-reset`.

## The `/wise-init` gate

The `/wise-insights-*` commands refuse to run until `/wise-init` has been run.
Each skill's first step runs `init-registry.py check`; unless it prints
`INIT:ok` the command tells you to run `/wise-init` and stops (it never installs
anything inline — `/wise-init` is the entry point). The shared guard is
`references/insights-init-guard.md`. The SessionEnd capture hook is the one
exception — it stays init-independent so the ledger fills in the background.

## Engine CLI reference

`scripts/insights.py` (stdlib-only) is the seam the skills shell out to. Skills
drive it; you can also run it directly for inspection or scripting.

| Subcommand | Purpose |
|---|---|
| `ingest <transcript> [--session-id <id>]` | Upsert one session into the ledger (the hook's call). |
| `mine [--here] [--since <Nd>] [--min-count <N>] [--include-automated] [--json]` | Self-heal, cluster, gate; rewrite candidates. |
| `list-candidates [--json]` | Dump the candidate store. |
| `show-candidate <cluster_id> [--json]` | Full evidence for one candidate. |
| `mark <cluster_id> <promoted\|dismissed> [--skill-name <n>] [--skill-path <p>]` | Record a decision. |
| `list-skills [--json] [--skills-dir <p>]` | Enumerate skills; classify managed vs external. |
| `overlap [--json] [--min-jaccard <f>] [--include-external] [--skills-dir <p>]` | Pairwise overlap edges. |
| `retire <skill_path> [--superseded-by <name>] [--json]` | Marker-gated backup-then-remove. |
| `reset [--skills] [--index] [--dry-run] [--skills-dir <p>] [--json]` | Snapshot then clear (reversible). |
| `list-snapshots [--json]` | Restore points, newest first. |
| `restore <ts> [--skills-dir <p>] [--json]` | Roll a restore point back. |
| `purge [--yes]` | **Irreversible** wipe of the whole insights store. |
| `data-root` | Print `…/insights` (the shell-side path seam). |

**Determinism.** `mine` and `overlap` are deterministic — same inputs ⇒
byte-identical output (sorted iteration, sha1 ids, no randomness/network).
`ingest`, `retire`, `reset`, `restore`, and `purge` are the documented
filesystem/timestamp exceptions.

## Testing / sandboxing

Two environment overrides let you exercise the whole subsystem without touching
real state — point them at throwaway dirs:

- `XDG_DATA_HOME` redirects the data root (so the ledger/snapshots land in temp).
- `WISE_SKILLS_DIR` redirects the skills dir (so `list-skills` / `overlap` /
  `retire` / `reset` / `restore` operate on a fixture set, not `~/.claude/skills`).

```bash
export XDG_DATA_HOME=/tmp/wise-sbx WISE_SKILLS_DIR=/tmp/wise-sbx-skills
mkdir -p /tmp/wise-sbx-skills
cd harnesses/claude/wise

python3 scripts/insights.py mine --since 30d --json     # mine real transcripts, ledger → /tmp
python3 scripts/insights.py list-skills --json          # fixture skills only
python3 scripts/insights.py reset --dry-run             # plan only, nothing changes
# clean up:  rm -rf /tmp/wise-sbx /tmp/wise-sbx-skills
```

This is the safe way to see what mine detects in your history and what overlap
finds, before installing the plugin and running the commands for real.

## Related

- [`skills-authoring.md`](./skills-authoring.md) — how wise skills are shaped
  (the learned skills follow the same conventions, with `user-invocable: false`).
- [`dispatcher.md`](./dispatcher.md) — how the `/wise` helper discovers and
  catalogs skills (it auto-consults drafted/refined skills when their
  `description` matches).
- `CONTRIBUTING.md` §2.4 (the SessionEnd hook exception) and §5 (plugin state).
