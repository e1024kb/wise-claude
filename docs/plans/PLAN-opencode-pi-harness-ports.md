# opencode-pi-harness-ports — Add opencode and Pi harness ports

Source: prompt — "we forgot to add 2 more harnesses:
https://github.com/anomalyco/opencode and Pi
https://github.com/earendil-works/pi — prepare implementation plan,
implementation delegated to Opus."

> **Revised after the full-functionality-installs PR (v3.4.0).** Ports
> now bake the default shared root into executable bash contexts
> (`${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/<h>}`
> — see `CONTRIBUTING.md` §10.3), carry a shared-file-resolution
> preamble per referencing skill, and ship an adapted `wise-init` (26
> skills per port: 11 full + 15 adapted; 6 Claude-only). `install.sh`
> lays the whole intact pack (5 dirs incl. `skills/`) at the shared
> root and validate_repo/install-smoke enforce all of this. Versions
> shift: opencode = **v3.5.0**, Pi = **v3.6.0**. Read the affected
> sections below with those deltas applied.

## Summary

Add two hand-maintained ports of the `wise` copilot, following the
v3.x model (`core/` canonical + per-harness vendored port, PRs
#31–#36):

- **`harnesses/opencode/wise/`** — for [opencode](https://github.com/anomalyco/opencode)
  (docs: https://opencode.ai/docs). Ships the standard 26-skill port
  **plus** two opencode-native extras: thin `commands/` wrappers (slash
  UX) and real subagent role cards (`agents/*.md` with opencode
  frontmatter) — opencode has a Task tool with parallel subagents, so
  the workflow conductor stays **parallel** (Claude/Hermes-like).
- **`harnesses/pi/wise/`** — for [Pi](https://github.com/earendil-works/pi)
  (`@earendil-works/pi-coding-agent`, docs in
  `packages/coding-agent/docs/`). Standard 26-skill port; skills are
  natively user-invocable as `/skill:<name>`, so no wrappers needed. Pi
  has **no built-in subagents** → conductor runs **sequential**
  (Cursor-like). Distribution bonus: a root `package.json` `"pi"` key
  makes `pi install git:github.com/<owner>/wise-claude` a one-command
  canonical install.

Two PRs, one per port (mirrors #33/#34): opencode → **v3.5.0**, Pi →
**v3.6.0** (CI version-bump gate forces a bump per PR touching
`harnesses/`).

## Assumptions

| # | Assumption | Confidence |
|---|---|---|
| A1 | Same skill tiering as codex/cursor/hermes: 26 skills shipped (11 full + 15 adapted incl. wise-init), 6 Claude-only skills omitted (`wise-supervise`, `wise-insights-{mine,refine,reset}`, `wise-skills-{create,edit}`). | high |
| A2 | opencode port ships `commands/` wrappers (one thin markdown command per user-invocable skill) because opencode skills are description-triggered only and wise UX is slash-centric. | medium-high |
| A3 | opencode port ships the 13 role cards as real opencode agents (`agents/*.md`, `mode: subagent`, `permission:` frontmatter) so `agent:`-bound workflow steps dispatch to real personas. | medium-high |
| A4 | Repo-root `package.json` with only a `"pi"` key (skills/prompts globs pointing into `harnesses/pi/wise/`) is acceptable at repo root; it carries a `version` field kept equal to the plugin version. | medium |
| A5 | Acceptance = repo loop green (validate, drift 0, 83 tests, CI) + `install.sh` works for both names. Real-harness smoke tests remain user-side, as for the first four ports. | high |

## Decisions Made

| Decision | Rationale | Source |
|---|---|---|
| opencode skills install dir: `~/.config/opencode/skills/` | opencode's own global dir; do NOT use `~/.agents/skills` — codex port already owns it and opencode reads it as a compat dir (would cross-wire ports whose adaptation notes name the wrong harness). | https://opencode.ai/docs/skills/ |
| Pi skills install dir: `~/.pi/agent/skills/` | Pi's global dir; same isolation argument vs `~/.agents/skills`. | pi repo `packages/coding-agent/docs/skills.md` |
| opencode conductor: parallel | opencode subagents + Task tool, "General subagent can run multiple units of work in parallel". | https://opencode.ai/docs/agents |
| Pi conductor: sequential | Pi deliberately ships no subagents (4 tools: read/write/edit/bash); extensions could add them but we don't depend on third-party `pi-subagents`. | pi `packages/coding-agent/README.md`, `docs/extensions.md` |
| No opencode marketplace manifest | opencode has no marketplace; plugins are npm/JS (a different mechanism). Install = `install.sh opencode` or manual copy. | https://opencode.ai/docs/plugins |
| Pi gets a package manifest (root `package.json` `"pi"` key) | `pi install git:` is Pi's canonical third-party install — the codex-marketplace analogue. | pi `docs/packages.md` |
| Session id: no shim needed | Neither harness exposes a session-id env var (opencode issue #9292 closed not-planned; Pi docs silent). Engine's synthetic id (`local-<cwd-slug>`) already covers both; `WISE_SESSION_ID` stays the documented override. | opencode issue #9292; `core/scripts/workflows.py` |
| Two PRs, one per port | Matches #33 (codex) / #34 (cursor+hermes) granularity; keeps review + version bumps clean. | git history |

## Harness fact sheets (for the implementer)

### opencode
- Skills: Agent Skills spec. Frontmatter read: `name` (required,
  `^[a-z0-9]+(-[a-z0-9]+)*$`, ≤64), `description` (required, ≤1024);
  optional `license`, `compatibility`, `metadata`. **Keep frontmatter to
  name + description** (port convention).
- Skills are loaded on demand via a native `skill` tool —
  **description-triggered only**, no slash form.
- Commands: `.opencode/commands/<name>.md` (project) /
  `~/.config/opencode/commands/` (global). Filename = `/name`.
  Frontmatter: `description`, optional `agent`, `model`, `subtask`.
  Body = prompt template ($ARGUMENTS-style args supported).
- Agents: `.opencode/agents/*.md` / `~/.config/opencode/agents/*.md`.
  Frontmatter: `description` (required), `mode`
  (`primary|subagent|all`), optional `model`, `temperature`,
  `permission` (NOT `tools` — deprecated). Subagents invoked via Task
  tool or `@name` mention; parallel execution supported.
- Rules: `AGENTS.md` (preferred) walked up from cwd; global
  `~/.config/opencode/AGENTS.md`.
- Config: `opencode.json`/`.jsonc`. No session-id env var. Bash tool: yes.

### Pi
- Skills: Agent Skills spec (lenient). Frontmatter: `name` (required,
  lowercase/digits/hyphens ≤64), `description` (≤1024); optional
  `license`, `compatibility`, `metadata`, `allowed-tools`,
  `disable-model-invocation`. Discovery: `~/.pi/agent/skills/`,
  `~/.agents/skills/`, `.pi/skills/`, `.agents/skills/` (recursive).
- Skills are model-invoked AND user-invocable as `/skill:<name>`
  (`enableSkillCommands` setting) → **no command wrappers needed**.
- Prompt templates: `~/.pi/agent/prompts/*.md` / `.pi/prompts/*.md`,
  invoked `/<filename>`, frontmatter `description` + `argument-hint`,
  bash-style `$1`/`$ARGUMENTS`. (Not used by this plan; skills suffice.)
- Packages: `pi install npm:<pkg>` / `git:github.com/<owner>/<repo>@<ref>`;
  resources auto-discovered from `skills/`, `prompts/`, `extensions/`,
  `themes/` dirs or declared in `package.json` under `"pi"` (globs +
  `!exclusions`).
- No subagents, no permission system, no hooks (extensions = TS event
  handlers; out of scope). Reads `AGENTS.md` (or `CLAUDE.md` fallback).
- Config: `~/.pi/agent/settings.json` / `.pi/settings.json`. Env Pi
  reads: `PI_CODING_AGENT_DIR`, `PI_CODING_AGENT_SESSION_DIR`, …; it
  exports no session env var into bash shells. Bash tool: yes.

## Tasks

Reference port for adaptation mechanics: `harnesses/hermes/wise/`
(parallel conductor — copy for opencode) and `harnesses/cursor/wise/`
(sequential conductor — copy for Pi). Port rules enforced by
`scripts/validate_repo.py::check_ports` (`harnesses/*/wise` glob — new
ports are auto-discovered): skill `name:` == dir name; **zero**
`${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}` literals in port
md/yaml — use `${WISE_PLUGIN_ROOT}` / `${WISE_DATA_DIR}`.

### PR A — opencode port (v3.5.0)

**Wave A1 (parallelizable)**
- [ ] **A1.1 — Vendor shared assets** (3 SP). Create
  `harnesses/opencode/wise/{references,workflows,scripts}/` from
  `core/` exactly as the hermes port does (`mode: adapted` — path
  rewrites `${CLAUDE_PLUGIN_ROOT}`→`${WISE_PLUGIN_ROOT}`; include
  `bootstrap-deps.sh`, `init-registry.py` like the other ports;
  workflow READMEs keep port-relative doc links). Reuse: diff
  `harnesses/hermes/wise/{references,workflows,scripts}` vs `core/` to
  extract the exact rewrite set; New: `harnesses/opencode/wise/` tree +
  `.gitignore` copied from hermes.
- [ ] **A1.2 — Adapt 26 skills** (5 SP). Copy the hermes port's 26
  skills; rewrite each "Harness adaptation note" for opencode: Task /
  subagent dispatch → "spawn a subagent via the Task tool / `@wise-<role>`
  mention with the role card in this pack's `agents/`; parallel
  dispatch supported"; AskUserQuestion → plain-chat question. Keep
  frontmatter name+description only. The `wise-workflow-run` conductor
  execution note says **parallel subagents** (hermes wording, opencode
  tools). Reuse: hermes skills as base (11 full skills are byte-close
  to core); New: 25 dirs under `harnesses/opencode/wise/skills/`.
- [ ] **A1.3 — Role cards as opencode agents** (2 SP). 13 files
  `harnesses/opencode/wise/agents/wise-<role>.md`: neutral `core/agents/`
  persona prose + opencode frontmatter `description:` (from the neutral
  card) and `mode: subagent`. No `tools:` (deprecated); omit
  `permission` (inherit). NOTE: because frontmatter is added, register
  these in `core-map.yaml` as `mode: adapted` (the other ports'
  `agents/` are `verbatim` — opencode diverges; A1.5 handles the map).

**Wave A2 (after A1.2)**
- [ ] **A2.1 — Command wrappers** (2 SP). For each skill that is
  user-invocable on Claude (has `argument-hint` in the claude port —
  the standalone set), add
  `harnesses/opencode/wise/commands/wise-<action>.md`: frontmatter
  `description:` (first sentence of the skill description); body =
  "Load and follow the `wise-<action>` skill with arguments:
  $ARGUMENTS". Reference skills (e.g. `wise-estimation`) get no
  wrapper. New: ~22 tiny files.
- [ ] **A2.2 — Port README** (1 SP). Structure mirrors
  `harnesses/hermes/wise/README.md`: install via `./install.sh opencode`
  (copies skills → `~/.config/opencode/skills/`, commands →
  `~/.config/opencode/commands/`, agents → `~/.config/opencode/agents/`,
  shared assets → `$WISE_HOME/harness/opencode` + `WISE_PLUGIN_ROOT`
  export) or manual copy; prerequisites (git, gh, python3+pyyaml+ulid,
  `WISE_PLUGIN_ROOT`); tiers Full 11 / Adapted 15 / Claude-only 6 not
  shipped; conductor note (parallel).

**Wave A3 (after A1/A2)**
- [ ] **A3.1 — Integration wiring** (3 SP).
  - `core/core-map.yaml`: add an opencode entry to all 6 mappings
    (`adapted` everywhere, incl. `agents/` per A1.3).
  - `install.sh`: add `opencode` to the validation case (~line 49) and
    `skills_target()` (~line 64 → `$HOME/.config/opencode/skills`);
    extend the copy step to also place `commands/` and `agents/` into
    `~/.config/opencode/{commands,agents}/` for opencode (new
    per-harness hook — keep the existing harnesses' behavior
    byte-identical); update usage strings (~lines 18, 36).
  - `justfile`: update the harness examples comment.
  - `docs/compatibility.md`: add an "opencode" column (rows match
    codex/hermes: ✓/~ per tier), update the workflows/parallelism
    prose (opencode = parallel).
  - Docs enumerations (add opencode wherever the four harnesses are
    listed): `README.md` (~lines 3, 7, 29, 43-48, 102-103, 118-129,
    155-157), `CLAUDE.md` (~4-5, 27), `CONTRIBUTING.md` (~21, 33-34,
    987), `core/README.md` (~22-23), `docs/wise/workflows.md`
    (~42-46, 167-171), `docs/wise/insights.md` (~5),
    `docs/wise/skills-authoring.md` (~260). Line numbers are as of
    v3.3.1 — re-locate by content.
- [ ] **A3.2 — Ship** (1 SP). Bump `harnesses/claude/wise/.claude-plugin/plugin.json`
  + `harnesses/codex/wise/.codex-plugin/plugin.json` → **3.5.0**. Run
  `just check` (validate + drift + tests); expect drift 0 and 83 tests.
  Branch → PR `feat(wise): add opencode port (v3.5.0)` → CI green →
  squash-merge.

### PR B — Pi port (v3.6.0)

**Wave B1 (parallelizable)**
- [ ] **B1.1 — Vendor shared assets** (2 SP). Same as A1.1 but base on
  the **cursor** port (sequential-conductor sibling). New:
  `harnesses/pi/wise/{references,workflows,scripts}/` + `.gitignore`.
- [ ] **B1.2 — Adapt 26 skills** (4 SP). Copy the cursor port's 26
  skills; rewrite adaptation notes for Pi: Task/subagent dispatch →
  "Pi has no subagents — adopt the role card at
  `${WISE_PLUGIN_ROOT}/agents/<role>.md` yourself and perform the
  steps sequentially"; AskUserQuestion → plain chat. Mention skills are
  invocable as `/skill:wise-<action>` where the claude doc says
  `/wise-<action>`. Conductor note: **sequential** (cursor wording).
- [ ] **B1.3 — Neutral agent cards** (1 SP). Vendor `core/agents/`
  verbatim to `harnesses/pi/wise/agents/` (cursor/hermes pattern —
  prose personas the model adopts inline).

**Wave B2 (after B1)**
- [ ] **B2.1 — Pi package manifest** (2 SP). Root `package.json`:
  `{"name": "wise-claude", "version": "3.6.0", "private": true,
  "pi": {"skills": ["harnesses/pi/wise/skills/*"]}}` so
  `pi install git:github.com/<owner>/wise-claude` works. Verify glob
  form against `docs/packages.md` semantics; exclude nothing else.
  Extend `scripts/validate_repo.py` version-parity check (currently
  codex-only at ~line 298) to also compare root `package.json`
  `version` when present. Extend the CI JSON-parse step to include it.
  NOTE: `pi install` won't set `WISE_PLUGIN_ROOT` — the port README
  must tell package users to export it (or use `install.sh pi`, which
  does).
- [ ] **B2.2 — Port README** (1 SP). Install: (a) `pi install
  git:github.com/<owner>/wise-claude` (skills only — export
  `WISE_PLUGIN_ROOT` manually per the note in B2.1), (b)
  `./install.sh pi` (full: skills → `~/.pi/agent/skills/`, shared →
  `$WISE_HOME/harness/pi`). Prereqs; tiers 11/15/6; conductor note
  (sequential).

**Wave B3 (after B1/B2)**
- [ ] **B3.1 — Integration wiring** (2 SP). `core-map.yaml` +6 pi
  entries (`adapted` for references/workflows/scripts-by-path… match
  the cursor pattern: engine scripts `verbatim`, references/workflows
  `adapted`, agents `verbatim`); `install.sh` case arms
  (`pi` → `$HOME/.pi/agent/skills`) + usage; `docs/compatibility.md`
  pi column + prose (sequential); same docs enumerations as A3.1 (now
  six harnesses).
- [ ] **B3.2 — Ship** (1 SP). Version → **3.6.0** (claude + codex
  manifests + root package.json). `just check`; PR
  `feat(wise): add Pi port (v3.6.0)`; CI green; squash-merge.

**Total: ~30 SP** (PR A ≈ 17, PR B ≈ 13).

## Testing

- `python3 scripts/validate_repo.py` — new ports auto-picked-up by the
  `harnesses/*/wise` glob: skill-name/dir parity + zero
  `${CLAUDE_PLUGIN_*}` literals. This is the primary correctness gate.
- `python3 scripts/report_core_drift.py` — after each core-map edit:
  expect **0 DRIFTED** (opencode `agents/` and both ports'
  references/workflows are `adapted` → skipped; engine scripts
  `verbatim` → byte-diffed).
- `python3 -m pytest harnesses/claude/wise/tests -q` — 83 pass
  (no engine changes in this plan; a failure means scope crept).
- `bash -n install.sh` + run `./install.sh opencode --user` /
  `./install.sh pi --user` into a temp `WISE_DATA_DIR` and assert the
  copied trees (CI already parses all `harnesses/*/wise/scripts/*.sh`).
- Grep gates: `grep -r 'CLAUDE_PLUGIN' harnesses/opencode harnesses/pi`
  → empty; CI stale-name grep covers `/wise:*` forms.
- Real-harness smoke (user-side, post-merge): install on actual
  opencode + Pi; confirm skill discovery, `commands/` wrappers
  (opencode), `/skill:wise-*` invocation (Pi), one workflow run each.

## Validation

- [ ] `just check` green after each PR (validate + drift + 83 tests)
- [ ] CI green: version-bump gate satisfied (3.4.0 / 3.5.0), bash/py
      parse steps cover the new ports via globs
- [ ] `docs/compatibility.md` has 6 harness columns, all 32 rows filled
- [ ] Every doc enumeration from A3.1's list names all six harnesses
- [ ] No open drift: `report_core_drift.py` prints 0 drifted
