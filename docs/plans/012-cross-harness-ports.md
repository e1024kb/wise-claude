# Cross-harness wise-claude: hand-maintained ports + shared core + universal installer

## Context

wise-claude is a Claude Code plugin marketplace (single plugin `wise`: 32 skills, 13 roster agents, 1 SessionEnd hook, 4 YAML workflows + a 1882-line Python engine `workflows.py`, 21 shared reference docs). Goal: install and use the plugin — **skills AND workflows** — in four harnesses: Claude Code, OpenAI Codex CLI, Cursor, Nous Research Hermes Agent (github.com/NousResearch/hermes-agent).

Research (2026-07-09):
- **All three targets adopted the Agent Skills standard** (SKILL.md, agentskills.io). Codex: `.agents/skills` / `~/.agents/skills`; Cursor: `.agents/skills`, `.cursor/skills`, `~/.cursor/skills` (+ native `disable-model-invocation` field); Hermes: `~/.hermes/skills`.
- **Codex has a Claude-look-alike plugin system**: `.codex-plugin/plugin.json` manifest, marketplace catalog at `$REPO_ROOT/.agents/plugins/marketplace.json`, installed via `codex plugin marketplace add`. (Docs thin — keep plain-skills fallback.)
- **Subagents**: Codex `[agents]` config (thin docs); Hermes native parallel subagents; Cursor — none confirmed (sequential fallback).
- The workflow **engine** is a plain Python CLI (pyyaml + python-ulid + typing_extensions) driven via shell — portable as-is. Only the **conductor** (`wise-workflow-run` SKILL.md) is Claude-specific prose.

User decisions (fixed):
1. **No build-time generation.** Per-harness versions are hand-maintained, committed, and supported — separate folder per harness. Users can go to a specific folder and install canonically (Claude/Codex marketplace commands), or use a universal CLI installer.
2. **Symmetric layout**: everything moves under `harnesses/` — including the Claude version. Backward incompatible → **major version 3.0.0**, migration described in README.
3. **`core/` + manual vendoring**: `core/` holds canonical harness-neutral assets; each harness folder keeps its own vendored copy, synced by hand; CONTRIBUTING documents the sync checklist.
4. **CI**: structural validation per harness folder + **non-blocking** core-vs-port divergence report.
5. Workflows must work in all four harnesses (functional parity; reduced parallelism/UX acceptable).
6. Installer: `install.sh` + `just` recipes (justfile; just.systems documented as optional dependency).

## Target layout

```
wise-claude/
├── .claude-plugin/marketplace.json      # STAYS at repo root (Claude requires it there);
│                                        # plugin source updated → ./harnesses/claude/wise
├── .agents/plugins/marketplace.json     # NEW — Codex marketplace catalog (root path required
│                                        # by codex plugin marketplace add) → ./harnesses/codex/wise
├── core/                                # canonical harness-neutral source (not installable itself)
│   ├── references/                      # 21 shared prose docs (grill/, pr/, root)
│   ├── agents/                          # 13 role cards, neutral form (name + description + persona prose;
│   │                                    #   no tools/model/effort/color frontmatter)
│   ├── workflows/                       # 4 workflow defs (workflow.yaml + prompts/)
│   ├── scripts/                         # workflows.py engine (+ engine.py/engine.sh)
│   └── core-map.yaml                    # NEW — maps each core path → vendored counterpart(s)
│                                        #   per harness; drives the drift report
├── harnesses/
│   ├── claude/wise/                     # current plugins/wise moved verbatim (git mv) —
│   │                                    #   .claude-plugin/plugin.json, skills/, agents/, hooks/,
│   │                                    #   workflows/, references/, scripts/, tests/
│   ├── codex/wise/                      # hand-maintained Codex port
│   │   ├── .codex-plugin/plugin.json    # name, version, description, skills path
│   │   ├── skills/<name>/SKILL.md       # adapted frontmatter + harness-adaptation preamble
│   │   │   └── agents/openai.yaml       # only where implicit invocation must be off
│   │   ├── workflows/  scripts/  references/  agents/   # vendored from core
│   │   └── README.md                    # canonical install: codex plugin marketplace add …
│   ├── cursor/wise/                     # hand-maintained Cursor skills pack
│   │   ├── skills/<name>/SKILL.md       # keeps Cursor-native disable-model-invocation
│   │   ├── workflows/ scripts/ references/ agents/
│   │   └── README.md                    # install: copy to .agents/skills / ~/.cursor/skills
│   └── hermes/wise/                     # hand-maintained Hermes pack
│       ├── skills/<name>/SKILL.md       # minimal frontmatter (name + description only)
│       ├── workflows/ scripts/ references/ agents/
│       └── README.md                    # install: copy to ~/.hermes/skills
├── install.sh                           # universal CLI installer (pure copier / marketplace-cmd wrapper)
├── justfile                             # just install codex | just validate | just test | just drift
├── scripts/validate_repo.py             # extended: per-harness structural validation
├── scripts/report_core_drift.py         # NEW — non-blocking core↔vendored divergence report
└── docs/compatibility.md                # hand-maintained skill/workflow × harness matrix
```

Constraint note: `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json` must physically stay at repo root — both harnesses resolve marketplaces from the repo root. "Move marketplace" = plugin content moves; root manifests only update their `source` paths.

## Port content rules (hand-maintained, initially authored in this series)

**Skill portability tiers** (documented in `docs/compatibility.md`, maintained by hand):
- **Full** (11 — pure prose + git/gh bash): wise-commit-message, wise-commit, wise-commit-push, wise-pr-create(-auto), wise-pr-add-reviewers, wise-pr-request-review-auto, wise-estimation, wise-feedback, wise-prd-architect, wise-trd-architect.
- **Adapted** (14 — Claude tool mentions get per-harness substitutions): wise-grill, wise-revise, wise-pr-watch(-auto), wise-code-review-auto, wise-simplify-auto, wise-implement-plan-auto, wise (dispatcher → catalog-only), and the six wise-workflow-* skills.
- **Claude-only** (7 — documented with reasons): wise-supervise (background-team watchdog), wise-insights-{mine,refine,reset} (SessionEnd hook + Claude transcript format), wise-skills-create/edit (delegate to Claude's skill-creator), wise-init (replaced per pack by a prerequisites section in the pack README: git, gh authed, python3 + pyyaml/ulid for workflows).

**Frontmatter policy per port**: keep `name` + `description` everywhere; drop `allowed-tools`, `argument-hint`, `model`, `effort`; `disable-model-invocation` → Cursor keeps as-is, Codex expresses via `agents/openai.yaml` (`allow_implicit_invocation: false`), Hermes drops. Strip "(canonical) `/wise:wise-x`" phrasing from descriptions.

**Path rewrites inside port bodies**: `${CLAUDE_PLUGIN_ROOT}/references/x.md` → pack-relative `references/x.md` (Codex plugin root / pack root for Cursor & Hermes); `${CLAUDE_PLUGIN_DATA}` → `${WISE_DATA_DIR:-$HOME/.local/share/wise}`.

**Harness-adaptation preamble** (first body section of every Adapted skill, per-harness wording):
- Task / `subagent_type: wise:<role>` → spawn a subagent with the role card at `agents/<role>.md` (Codex/Hermes) / adopt the role yourself and work sequentially (Cursor).
- AskUserQuestion → ask the user in plain chat and wait.
- Skill tool → open and follow the named skill's SKILL.md directly.
- TodoWrite → keep a visible checklist in replies.

## Workflows — parity in all four harnesses

1. **Engine neutralization** (`core/scripts/workflows.py`, vendored to all harness folders; backward compatible for Claude):
   - `${CLAUDE_PLUGIN_DATA}` → fall back to `$WISE_DATA_DIR`, then `~/.local/share/wise`.
   - Session-transcript subcommands (`session-path`, `current-session-id`, `find-runs-by-session`) degrade gracefully outside Claude: synthetic session id per run; `find-runs-by-session` returns empty instead of erroring. Resume must not require transcript access outside Claude.
2. **Conductor adaptation** in each port's `wise-workflow-run`/`-resume` — per-harness execution-primitive mapping replacing Claude tool references:

   | Step type | Claude | Codex | Hermes | Cursor |
   |---|---|---|---|---|
   | `bash` | Bash | shell | shell | shell |
   | `skill` | Skill tool | open + follow `skills/<name>/SKILL.md` | same | same |
   | `prompt` (agent) | Task `wise:<role>` | subagent w/ role card, sequential fallback | native subagent w/ role card | sequential in-context role adoption |
   | `prompt` (team) | parallel Task team | parallel if available, else sequential per role | parallel subagents | sequential per role |
   | `ask` / `approval` | AskUserQuestion | plain-chat question | same | same |
   | `interactive` | conversation takeover | same | same | same |
   | `supervised-prompt` | Task + supervise watchdog | plain `prompt` (no watchdog), noted in matrix | same | same |

3. All 4 workflow defs vendored into every port; `workflow.yaml` schema unchanged (conductor maps `agent:`/`model`/`effort` per harness; model/effort ignored where unsupported).
4. MCP-server repackaging rejected for v1 — engine is already shell-driven, which all four harnesses have. Fallback if smoke tests show a harness can't drive the CLI reliably.

## Hooks & insights

No hooks in non-Claude ports (the only hook feeds insights; insights assumes Claude transcript JSONL). Matrix: "self-improvement loop: Claude Code only". Phase-2 backlog: Cursor sessionEnd adapter + transcript abstraction.

## core/ vendoring & drift report

- `core/` is the canonical source; the Claude port's existing `references/`, `agents/`, `workflows/`, `scripts/` become its first vendored copies (core extracted FROM them, then both kept in sync manually).
- `core/core-map.yaml`: explicit map `core path → [vendored paths per harness]` (some vendored files legitimately diverge — e.g. adapted conductor prose — those map with `mode: adapted` and are excluded from byte-diff, listed as "manually verify").
- `scripts/report_core_drift.py`: reads core-map.yaml, byte-diffs `mode: verbatim` entries, prints a summary table (in-sync / drifted / adapted-skip); **always exits 0**. Run in CI as a visible non-blocking step and via `just drift`.
- CONTRIBUTING gains a sync checklist: edit `core/` first → propagate to each harness folder → run `just drift` → update `docs/compatibility.md` if tiers changed.

## Universal installer

**`install.sh`** (repo root, bash 3.2-compatible, pure copier / command wrapper — never transforms files):

| Target | `--user` (default) | `--project <dir>` |
|---|---|---|
| claude | `claude plugin marketplace add e1024kb/wise-claude && claude plugin install wise@wise-claude` if `claude` on PATH, else print commands | print guidance |
| codex | try `codex plugin marketplace add <repo>`; fallback `cp -R harnesses/codex/wise/skills/* ~/.agents/skills/` | cp into `<dir>/.agents/skills/` |
| cursor | `cp -R harnesses/cursor/wise/skills/* ~/.cursor/skills/` | cp into `<dir>/.agents/skills/` |
| hermes | `cp -R harnesses/hermes/wise/skills/* ~/.hermes/skills/` | n/a |

Behaviors: refuse to overwrite differing existing skill dirs without `--force`; `--uninstall` removes exactly what it installed; post-install prints compatibility summary + prerequisites. Document the Cursor double-install footgun (marketplace plugin + copied pack → duplicates; pick one).

**`justfile`**:
```just
default: validate test
validate:  python3 scripts/validate_repo.py
test:      python3 -m pytest harnesses/claude/wise/tests
drift:     python3 scripts/report_core_drift.py
install harness scope="user" project=".": ./install.sh {{harness}} --{{scope}} --project {{project}}
```

## Validation & CI

- `scripts/validate_repo.py` extended:
  - existing checks re-pointed at `harnesses/claude/wise` (path constant change);
  - per-port structural checks: every `harnesses/*/wise/skills/*/SKILL.md` frontmatter parses + `name` === dir; no literal `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_PLUGIN_DATA}` in non-Claude ports; relative refs (`references/…`, `agents/…`, `workflows/…`) resolve within the port; `.codex-plugin/plugin.json` + both root marketplace manifests parse and `version` fields match the Claude plugin.json.
- `.github/workflows/ci.yaml`: path updates (`plugins/wise` → `harnesses/claude/wise` everywhere incl. version-bump gate + pytest path); new non-blocking `report_core_drift.py` step; version-bump gate extended to cover `harnesses/**` and `core/**`.
- Grep-gates for stale `plugins/wise` references in docs.

## Versioning & migration

- **3.0.0** (major — layout move). Single source: `harnesses/claude/wise/.claude-plugin/plugin.json`; Codex `.codex-plugin/plugin.json` + both marketplace manifests carry the same version, kept in sync by hand (validated by CI version-match check).
- README migration section: existing Claude installs — `claude plugin marketplace update` picks up the new source path automatically if the marketplace re-reads from git; otherwise document remove + re-add (`claude plugin uninstall wise` / `marketplace remove` / re-add). Verify actual behavior during smoke test and document precisely.

## Testing / verification

- Existing 8 pytest files move with the Claude port (`harnesses/claude/wise/tests`) — must stay green after the move (conftest loads workflows.py by path — update path).
- New engine tests: `WISE_DATA_DIR` fallback; non-Claude session-id degradation; resume without transcripts.
- New validator tests optional (validate_repo.py currently untested — keep to manual CI runs).
- Manual smoke tests (documented; fixes land in the final PR):
  - Claude: fresh `marketplace add` + install from the new layout; run `/wise-commit`, `/wise-workflow-run example-workflow`.
  - Codex: `codex plugin marketplace add` in scratch repo → `/wise-commit`; `example-workflow` end-to-end; fallback plain-skills path.
  - Cursor: pack in project `.agents/skills/` → discovery, `disable-model-invocation` respected, `/wise-pr-create`, sequential `example-workflow`.
  - Hermes: `~/.hermes/skills/` → `/skills` lists them, `/wise-commit-message`, `example-workflow` with native subagents.

## Risks

| Risk | Mitigation |
|---|---|
| Layout move breaks existing Claude installs | major version + README migration; smoke-test the update path before merging |
| Codex plugin/marketplace docs thin | codex port doubles as plain Agent Skills pack; installer fallback `~/.agents/skills` |
| Hermes frontmatter extensions unconfirmed | ship name+description only |
| Cursor lacks subagents → team steps sequential | acceptable per parity definition; documented per cell in matrix |
| Manual vendoring drifts silently | core-map.yaml + drift report in CI (visible, non-blocking) + CONTRIBUTING checklist |
| 4 hand-maintained copies of 32 skills = high maintenance | tiers keep non-Claude ports to 25 skills; drift report scopes review; future converter can be revisited without layout change |
| Harness LLMs drive workflows.py CLI poorly | example-workflow smoke test per harness; MCP repackaging documented fallback |

## PR slicing (each: implement → validate+tests → commit → PR → green → merge)

1. **PR 1 — layout move + core extraction (3.0.0)**: `git mv plugins/wise harnesses/claude/wise`; create `core/` (references, agents-neutral, workflows, scripts) + `core-map.yaml`; update `.claude-plugin/marketplace.json` source, `validate_repo.py` paths, CI paths, conftest path, README/CLAUDE.md/CONTRIBUTING; migration notes; version 3.0.0.
2. **PR 2 — engine neutralization**: `WISE_DATA_DIR` fallback + session degrade in `core/scripts/workflows.py` + vendored Claude copy; new engine tests.
3. **PR 3 — Codex port**: `harnesses/codex/wise/` (manifest, 25 skills adapted, vendored core assets, conductor mapping, README) + root `.agents/plugins/marketplace.json`; validator per-port checks.
4. **PR 4 — Cursor + Hermes ports**: `harnesses/cursor/wise/`, `harnesses/hermes/wise/` + READMEs.
5. **PR 5 — installer + docs**: `install.sh`, `justfile`, `scripts/report_core_drift.py` + CI step, `docs/compatibility.md`, README "Other harnesses", CONTRIBUTING sync checklist.
6. **PR 6 — smoke-test fixes**: whatever the four manual smoke tests surface.
7. **Phase-2 backlog**: Cursor sessionEnd hook adapter + insights transcript abstraction; Codex `[agents]` generation; wise-supervise port; Skills Hub / openskills listing.

## Critical files

- New: `core/**` (+ `core/core-map.yaml`), `harnesses/codex/wise/**`, `harnesses/cursor/wise/**`, `harnesses/hermes/wise/**`, `.agents/plugins/marketplace.json`, `install.sh`, `justfile`, `scripts/report_core_drift.py`, `docs/compatibility.md`.
- Moved: `plugins/wise/**` → `harnesses/claude/wise/**`.
- Modified: `.claude-plugin/marketplace.json` (source path), `scripts/validate_repo.py`, `.github/workflows/ci.yaml`, `README.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `core/scripts/workflows.py` + Claude vendored copy (engine neutralization), plugin.json (3.0.0).
