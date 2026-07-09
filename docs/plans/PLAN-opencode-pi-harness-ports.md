# opencode-pi-harness-ports — Add opencode and Pi harness ports

Source: prompt — "we forgot to add 2 more harnesses:
https://github.com/anomalyco/opencode and Pi
https://github.com/earendil-works/pi — prepare implementation plan,
implementation delegated to Opus."

> **Revised after the port-generator PR (v3.5.0).** Port content is now
> **generated**: `scripts/build_ports.py` renders every committed port
> tree (skills, references, workflows, agents, engine, init trio,
> `.gitignore`) from `core/` + `harnesses/claude/wise/skills/` +
> `core/ports/{profiles,notes,overlays,static}`, and
> `python3 scripts/build_ports.py --check` fails CI on any drift. A new
> harness is therefore **one profile + note overrides + overlays +
> statics + wiring**, not 26 hand-adapted skill dirs — the env-var
> rewrite, frontmatter reduction, invocation-prose strip, description
> reflow, preambles, and adaptation notes all come from the generator.
> Earlier revision context still applies (baked default root per
> CONTRIBUTING §10.3, shared-file-resolution preambles, adapted
> `wise-init`, 26 skills per port: 11 full + 15 adapted; 6 Claude-only).
> Versions shift again: the generator PR takes **v3.5.0**, so
> opencode = **v3.6.0**, Pi = **v3.7.0**. Read the affected sections
> below with those deltas applied.

## Summary

Add two ports of the `wise` copilot, following the v3.x model (`core/`
canonical + per-harness port, PRs #31–#36) — but authored as
**generator inputs** under `core/ports/`, with the committed
`harnesses/<h>/wise/` trees materialized by
`python3 scripts/build_ports.py`:

- **`harnesses/opencode/wise/`** — for [opencode](https://github.com/anomalyco/opencode)
  (docs: https://opencode.ai/docs). Ships the standard 26-skill port
  **plus** two opencode-native extras shipped as statics: thin
  `commands/` wrappers (slash UX) and real subagent role cards
  (`agents/*.md` with opencode frontmatter) — opencode has a Task tool
  with parallel subagents, so the workflow conductor stays **parallel**
  (Claude/Hermes-like → overlays and note overrides start from the
  hermes inputs).
- **`harnesses/pi/wise/`** — for [Pi](https://github.com/earendil-works/pi)
  (`@earendil-works/pi-coding-agent`, docs in
  `packages/coding-agent/docs/`). Standard 26-skill port; skills are
  natively user-invocable as `/skill:<name>`, so no wrappers needed. Pi
  has **no built-in subagents** → conductor runs **sequential**
  (Cursor-like → overlays and note overrides start from the cursor
  inputs, plus the pack-relative fallback sentence). Distribution
  bonus: a root `package.json` `"pi"` key makes
  `pi install git:github.com/<owner>/wise-claude` a one-command
  canonical install.

Two PRs, one per port (mirrors #33/#34): opencode → **v3.6.0**, Pi →
**v3.7.0** (CI version-bump gate forces a bump per PR touching
`harnesses/`).

## Assumptions

| # | Assumption | Confidence |
|---|---|---|
| A1 | Same skill tiering as codex/cursor/hermes: 26 skills shipped (11 full + 15 adapted incl. wise-init), 6 Claude-only skills omitted (`wise-supervise`, `wise-insights-{mine,refine,reset}`, `wise-skills-{create,edit}`) — expressed as the profile's `excluded_skills` / `note_skills` / `blockquote_skills` lists, copied from `core/ports/profiles/hermes.yaml`. | high |
| A2 | opencode port ships `commands/` wrappers (one thin markdown command per user-invocable skill) because opencode skills are description-triggered only and wise UX is slash-centric. Shipped as statics under `core/ports/static/opencode/commands/`. | medium-high |
| A3 | opencode port ships the 13 role cards as real opencode agents (`agents/*.md`, `mode: subagent` frontmatter). Because the generator's port-agents rule is a byte-copy of the neutral `core/agents/` cards, these frontmattered variants ship as statics under `core/ports/static/opencode/agents/` (statics win over / replace the byte-copy — confirm the generator's precedence and adjust if statics and generated agents would collide). | medium-high |
| A4 | Repo-root `package.json` with only a `"pi"` key (skills globs pointing into `harnesses/pi/wise/`) is acceptable at repo root; it carries a `version` field kept equal to the plugin version. | medium |
| A5 | Acceptance = repo loop green (validate, `build_ports.py --check` clean, 83 tests, CI) + `install.sh` works for both names. Real-harness smoke tests remain user-side, as for the first four ports. | high |
| A6 | `build_ports.py` picks up a new harness from its profile file (or a small hardcoded harness list — check the script; if hardcoded, adding the id is part of wiring). Existing three ports' output stays byte-identical after the addition. | high |

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
| Ports authored as generator inputs, never hand-edited under `harnesses/` | `build_ports.py --check` in CI rejects any hand edit to a generated tree; the profile/notes/overlays/statics under `core/ports/` are the only editable surface. | PLAN-port-generator.md; `scripts/build_ports.py` |

## Harness fact sheets (for the implementer)

### opencode
- Skills: Agent Skills spec. Frontmatter read: `name` (required,
  `^[a-z0-9]+(-[a-z0-9]+)*$`, ≤64), `description` (required, ≤1024);
  optional `license`, `compatibility`, `metadata`. **Keep frontmatter to
  name + description** (the profile's `frontmatter_keep`, same as the
  other ports).
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

Reference **generator inputs** for each new port: hermes (parallel
conductor — copy for opencode) and cursor (sequential conductor — copy
for Pi), i.e. `core/ports/profiles/{hermes,cursor}.yaml`,
`core/ports/notes/wise-workflow-{run,resume}.{hermes,cursor}.md`,
`core/ports/overlays/{hermes,cursor}/`, `core/ports/static/{hermes,cursor}/`.
The committed `harnesses/<h>/wise/` trees are produced by
`python3 scripts/build_ports.py` and must never be hand-edited
(`--check` in CI enforces this). Port rules still enforced by
`scripts/validate_repo.py::check_ports` (`harnesses/*/wise` glob — new
ports are auto-discovered): skill `name:` == dir name; **zero**
`${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}` literals in port
md/yaml — the generator's env rewrite guarantees this by construction.

Overlay mechanics reminder: hunks in `core/ports/overlays/<h>/<skill>.md`
use `<<<<<<< find / ======= / >>>>>>>` blocks anchored on
**post-env-rewrite** text and must match exactly once (generator
hard-errors otherwise). Note templates use `{{harness_name}}` /
`{{harness_id}}`; `notes/<skill>.<h>.md` overrides `notes/<skill>.md`.

### PR A — opencode port (v3.6.0)

**Wave A1 — generator inputs (parallelizable)**
- [ ] **A1.1 — Profile** (1 SP). `core/ports/profiles/opencode.yaml`:
  `id: opencode`, `name: opencode`, `frontmatter_keep: [name,
  description]`, and the `excluded_skills` / `note_skills` /
  `blockquote_skills` lists copied verbatim from `hermes.yaml` (same
  editorial tiering). Confirm whether `build_ports.py` discovers
  harnesses from the profiles dir or a hardcoded list; if the latter,
  add the id (A3.1 covers the rest of wiring).
- [ ] **A1.2 — Note overrides** (1 SP). The shared templated notes in
  `core/ports/notes/*.md` render for opencode automatically via
  `{{harness_name}}`/`{{harness_id}}` — review each rendered note and
  add `notes/<skill>.opencode.md` overrides **only** where the generic
  text misfires. Known-needed (conductor sections genuinely diverge per
  harness): `wise-workflow-run.opencode.md` and
  `wise-workflow-resume.opencode.md`, based on the **hermes** variants
  but with opencode dispatch wording — "spawn a subagent via the Task
  tool / `@wise-<role>` mention with the role card in this pack's
  `agents/`; parallel dispatch supported"; AskUserQuestion → plain-chat
  question.
- [ ] **A1.3 — Overlays** (1 SP). `core/ports/overlays/opencode/` —
  start from the hermes overlays (`wise-init.md`,
  `wise-workflow-create.md`), re-anchor the find-hunks on opencode's
  post-env-rewrite text (the defaulted expansion embeds
  `/harness/opencode`), adapt replacement prose. Add further overlays
  only if the generated-vs-intended diff demands them.
- [ ] **A1.4 — Statics** (3 SP). `core/ports/static/opencode/`:
  - `README.md` — structure mirrors the hermes port README: install via
    `./install.sh opencode` (skills → `~/.config/opencode/skills/`,
    commands → `~/.config/opencode/commands/`, agents →
    `~/.config/opencode/agents/`, intact pack at the shared root) or
    manual copy; prerequisites (git, gh, python3+pyyaml+ulid); tiers
    Full 11 / Adapted 15 / Claude-only 6 not shipped; conductor note
    (parallel).
  - `commands/wise-<action>.md` (~22 files) — for each skill
    user-invocable on Claude (has `argument-hint` in the claude port —
    the standalone set): frontmatter `description:` (first sentence of
    the skill description); body = "Load and follow the
    `wise-<action>` skill with arguments: $ARGUMENTS". Reference
    skills (e.g. `wise-estimation`) get no wrapper.
  - `agents/wise-<role>.md` (13 files) — neutral `core/agents/` persona
    prose + opencode frontmatter `description:` (from the neutral card)
    and `mode: subagent`. No `tools:` (deprecated); omit `permission`
    (inherit). These replace the generator's default byte-copied
    neutral cards — verify statics take precedence over the agents rule
    (or add a per-profile switch in `build_ports.py` if they collide;
    keep the other three ports byte-identical).

**Wave A2 (after A1)**
- [ ] **A2.1 — Generate + wiring** (2 SP).
  - Run `python3 scripts/build_ports.py` → materializes
    `harnesses/opencode/wise/` (26 skills, references, workflows,
    agents, engine trio, init trio, `.gitignore`, statics). Iterate on
    A1 inputs until the tree reads right; commit generated output.
  - `install.sh`: add `opencode` to the validation case (~line 51) and
    `skills_target()` (~line 72 → `$HOME/.config/opencode/skills`);
    extend the copy step to also place `commands/` and `agents/` into
    `~/.config/opencode/{commands,agents}/` for opencode (new
    per-harness hook — keep the existing harnesses' behavior
    byte-identical); update usage strings (~lines 15, 37).
  - `scripts/install_smoke.sh`: add an `opencode` case arm
    (`skills_dir="$H/.config/opencode/skills"`) and add it to the
    default harness list; assert the `commands/`+`agents/` copies too.
  - `scripts/validate_repo.py`: new port auto-discovered by the
    `harnesses/*/wise` glob — confirm no hardcoded harness list needs
    the new id; extend any per-port expectations (e.g. extra top-level
    `commands/` dir must not trip pack-shape checks).
  - `core/core-map.yaml`: if the map still tracks port copies on this
    branch, add opencode entries mirroring hermes; if the generator PR
    retired it, skip (build_ports.py's mapping subsumes it).
  - `justfile`: update the harness examples comment.
- [ ] **A2.2 — Docs** (1 SP). `docs/compatibility.md`: add an
  "opencode" column (rows match hermes: ✓/~ per tier), update the
  workflows/parallelism prose (opencode = parallel). Docs enumerations
  (add opencode wherever the four harnesses are listed): `README.md`,
  `CLAUDE.md`, `CONTRIBUTING.md`, `core/README.md`,
  `docs/wise/workflows.md`, `docs/wise/insights.md`,
  `docs/wise/skills-authoring.md` — locate by content, not line number.

**Wave A3 (after A1/A2)**
- [ ] **A3.1 — Ship** (1 SP). Bump **both** version sources:
  `harnesses/claude/wise/.claude-plugin/plugin.json` AND the static
  input `core/ports/static/codex/.codex-plugin/plugin.json` → **3.6.0**
  (the codex manifest is a byte-copied static — validate_repo enforces
  the match), then re-run `python3 scripts/build_ports.py`. Run
  `just check` (validate + build --check + tests); expect clean check
  and 83 tests. Branch → PR `feat(wise): add opencode port (v3.6.0)`
  → CI green → squash-merge.

### PR B — Pi port (v3.7.0)

**Wave B1 — generator inputs (parallelizable)**
- [ ] **B1.1 — Profile** (1 SP). `core/ports/profiles/pi.yaml`: `id:
  pi`, `name: Pi`, `frontmatter_keep: [name, description]`, tier lists
  copied from `cursor.yaml`.
- [ ] **B1.2 — Note overrides** (1 SP). Review the templated notes as
  rendered for Pi; add `notes/<skill>.pi.md` only where needed.
  Known-needed: `wise-workflow-run.pi.md` and
  `wise-workflow-resume.pi.md`, based on the **cursor** variants:
  "Pi has no subagents — adopt the role card at
  `${WISE_PLUGIN_ROOT}/agents/<role>.md` yourself and perform the
  steps sequentially"; AskUserQuestion → plain chat. Where a note
  mentions slash invocation, use Pi's `/skill:wise-<action>` form.
- [ ] **B1.3 — Overlays** (1 SP). `core/ports/overlays/pi/` — start
  from the cursor overlays, re-anchor on Pi's post-env-rewrite text
  (`/harness/pi` in the defaulted expansion), and add the
  pack-relative fallback sentence the cursor overlay carries.
- [ ] **B1.4 — Statics** (1 SP). `core/ports/static/pi/README.md`:
  install (a) `pi install git:github.com/<owner>/wise-claude` (skills
  only — export `WISE_PLUGIN_ROOT` manually per the B2.1 note), (b)
  `./install.sh pi` (full: skills → `~/.pi/agent/skills/`, intact pack
  at the shared root). Prereqs; tiers 11/15/6; conductor note
  (sequential). No wrappers, no frontmattered agents — the generator's
  byte-copied neutral cards suffice (cursor/hermes pattern).

**Wave B2 (after B1)**
- [ ] **B2.1 — Pi package manifest** (2 SP). Root `package.json`:
  `{"name": "wise-claude", "version": "3.7.0", "private": true,
  "pi": {"skills": ["harnesses/pi/wise/skills/*"]}}` so
  `pi install git:github.com/<owner>/wise-claude` works. Verify glob
  form against `docs/packages.md` semantics; exclude nothing else.
  Extend `scripts/validate_repo.py` version-parity check (currently
  claude↔codex-manifest) to also compare root `package.json` `version`
  when present. Extend the CI JSON-parse step to include it.
  NOTE: `pi install` won't set `WISE_PLUGIN_ROOT` — the port README
  must tell package users to export it (or use `install.sh pi`, which
  does).
- [ ] **B2.2 — Generate + wiring** (2 SP). Run
  `python3 scripts/build_ports.py` → materializes
  `harnesses/pi/wise/`; iterate on B1 inputs; commit. `install.sh`
  case arms (`pi` → `$HOME/.pi/agent/skills`) + usage;
  `scripts/install_smoke.sh` `pi` case arm + default list;
  `core/core-map.yaml` pi entries if the map survives (else skip);
  `docs/compatibility.md` pi column + prose (sequential); same docs
  enumerations as A2.2 (now six harnesses).

**Wave B3 (after B1/B2)**
- [ ] **B3.1 — Ship** (1 SP). Version → **3.7.0** in all three places:
  claude plugin.json, `core/ports/static/codex/.codex-plugin/plugin.json`,
  root `package.json` — then regenerate (`build_ports.py`).
  `just check`; PR `feat(wise): add Pi port (v3.7.0)`; CI green;
  squash-merge.

**Total: ~16 SP** (PR A ≈ 10, PR B ≈ 6–7). Down from ~30 SP
pre-generator: vendoring shared assets and hand-adapting 26 skills per
port are gone — `build_ports.py` materializes them from ~4–6 small
input files per harness.

## Testing

- `python3 scripts/build_ports.py --check` — the **primary** gate for
  port content: clean on the committed tree after each generate+commit;
  any hand edit under `harnesses/{opencode,pi}/wise/` fails it.
- `python3 scripts/validate_repo.py` — new ports auto-picked-up by the
  `harnesses/*/wise` glob: skill-name/dir parity + zero
  `${CLAUDE_PLUGIN_*}` literals + version parity (claude ↔ codex
  manifest, and root `package.json` after B2.1). Confirm no hardcoded
  four-harness list lurks in it; extend if so.
- `python3 -m pytest harnesses/claude/wise/tests -q` — 83 pass
  (no engine changes in this plan; a failure means scope crept).
- `bash scripts/install_smoke.sh opencode` / `... pi` — extend the
  script's case arms + default list (part of A2.1/B2.2), then assert
  the copied trees, including opencode's `commands/` + `agents/`
  destinations (CI already parses all `harnesses/*/wise/scripts/*.sh`).
- Grep gates: `grep -r 'CLAUDE_PLUGIN' harnesses/opencode harnesses/pi`
  → empty (guaranteed by the generator's env rewrite, but keep the
  gate); CI stale-name grep covers `/wise:*` forms.
- Real-harness smoke (user-side, post-merge): install on actual
  opencode + Pi; confirm skill discovery, `commands/` wrappers
  (opencode), `/skill:wise-*` invocation (Pi), one workflow run each.

## Validation

- [ ] `just check` green after each PR (validate + `build_ports.py
      --check` + 83 tests)
- [ ] CI green: version-bump gate satisfied (3.6.0 / 3.7.0), bash/py
      parse steps cover the new ports via globs
- [ ] `install_smoke.sh` covers all installable harnesses incl.
      opencode + pi
- [ ] `docs/compatibility.md` has 6 harness columns, all rows filled
- [ ] Every doc enumeration from A2.2's list names all six harnesses
- [ ] No hand edits under generated roots: `build_ports.py --check`
      exits 0 on the merged tree
