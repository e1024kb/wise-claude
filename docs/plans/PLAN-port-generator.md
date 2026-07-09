# port-generator — build-time generation of harness ports (v3.5.0)

## Summary

Replace hand-maintained propagation of port content with a deterministic
generator. Today ~25k lines across codex/cursor/hermes are near-copies:
cursor↔hermes skills differ by ~3% (name + path suffix), claude↔port
skills by ~10% (mechanical transforms), port references/workflows differ
from core ONLY by the env-var rewrite, port agents/scripts are core
byte-verbatim. A generator (`scripts/build_ports.py`) renders every port
from canonical sources + small per-harness profiles + overlays. Generated
output stays **committed** (installs, review, `pi install git:` unchanged);
CI regenerates and fails on diff. Drift becomes impossible by construction;
a new harness becomes one profile + overlays instead of 26 hand-adapted
files.

## Sources of truth (after this change)

1. `core/` — references, workflows, scripts, agents (neutral form; unchanged role)
2. `harnesses/claude/wise/skills/` — canonical skill bodies (Claude is the
   reference implementation; `wise-skills-create/edit` keep authoring here)
3. `core/ports/` — NEW: per-harness generation inputs
   - `core/ports/profiles/<h>.yaml` — harness id, display name, install
     command, default-root suffix, skills discovery dir, excluded skills
     (the 6 Claude-only), conductor capability (`parallel|sequential`),
     frontmatter policy, agent frontmatter data (claude only)
   - `core/ports/notes/<skill>.md` — adaptation-note template per adapted
     skill, with `{{harness_name}}`/`{{harness_id}}` vars; optional
     per-harness override `core/ports/notes/<skill>.<h>.md`
   - `core/ports/overlays/<h>/…` — genuinely divergent sections/files
     (wise-workflow-run conductor prose, wise-workflow-resume, wise-init
     variations)
   - `core/ports/static/<h>/…` — files with no neutral source
     (codex `openai.yaml`, port `README.md`s)

Everything else under `harnesses/` is **generated and committed**.

## Generated mapping

| Target | Source | Transform |
|---|---|---|
| `harnesses/claude/wise/references/` | `core/references/` | byte-copy |
| `harnesses/claude/wise/agents/` | `core/agents/` | + frontmatter lines from profile |
| `harnesses/claude/wise/{workflows,scripts}/` | core | byte-copy |
| `harnesses/<p>/wise/{scripts,agents}/` | core | byte-copy |
| `harnesses/<p>/wise/{references,workflows}/` | core | env-var rewrite (context-sensitive) |
| `harnesses/<p>/wise/skills/` | claude skills | pipeline below |
| static extras | `core/ports/static/<h>/` | byte-copy |

Claude skill files themselves are NOT generated (they are source).

### Skill transform pipeline (claude → port)

1. **Frontmatter**: keep `name` + `description`; drop `model`, `effort`,
   `allowed-tools`, `argument-hint`, `disable-model-invocation` etc. per
   profile policy; strip `(bare alias) or \`/wise:<name>\` (canonical)`
   invocation prose from the description; reflow deterministically.
2. **Env-var rewrite** (rules of CONTRIBUTING §10.3, already enforced by
   `validate_repo.py`): inside fenced `bash` blocks and quoted paths →
   `${WISE_PLUGIN_ROOT:-${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/<h>}`;
   in prose/Read refs → short `${WISE_PLUGIN_ROOT}`.
3. **Preamble**: full-tier skills get the blockquote "Shared-file
   resolution" line after the H1; adapted skills get the shared-files
   bullet inside the adaptation note (both templated from the profile).
4. **Adaptation note**: insert rendered `core/ports/notes/<skill>.md`
   (or the per-harness override).
5. **Overlays**: whole-file or marked-section replacement from
   `core/ports/overlays/<h>/`.
6. **Exclusions**: skip skills listed in the profile (6 Claude-only).

### Determinism

Pure functions of committed inputs; no timestamps. `build_ports.py --check`
regenerates to a temp dir and diffs against the tree (exit non-zero on
drift) — this replaces `report_core_drift.py` for generated files.

## Acceptance criterion

Regenerated tree ≈ current committed tree. Byte-parity is the target;
small **normalization diffs** (whitespace reflow, inconsistent hand-edits
the generator unifies) are acceptable when reviewed and listed in the PR
body. Behavior oracles must stay green: `validate_repo.py` (incl. §10.3
checks), 83 engine tests, `install_smoke.sh`, drift-free.

## Tasks (waves)

### Wave 1 — extraction specs (parallel, read-only)
- A1/A2/A3: for codex / cursor / hermes, inventory EVERY file differing
  from its source (claude skills or core) and classify each hunk:
  envvar | preamble | note | frontmatter | overlay | static. Emit the
  exact note texts, preamble texts, overlay sections, static file list.

### Wave 2 — generator (sequential, single owner)
- B1: write `scripts/build_ports.py` (stdlib + pyyaml, bash-free), author
  `core/ports/` (profiles, note templates, overlays, static) from the
  Wave-1 specs; **loop**: generate → diff vs committed → refine until only
  justified normalization diffs remain; commit regenerated tree.
  Constraint: engines, install.sh, core assets unchanged.

### Wave 3 — integration (parallel, disjoint files)
- C1: wiring — CI step `build --check`, `just build` + `check` recipe,
  retire `report_core_drift.py` + core-map (generator manifest subsumes
  them), update `validate_repo.py` core-map checks accordingly.
- C2: docs — CONTRIBUTING editing model (§ replace "propagate by hand"),
  CLAUDE.md structure/editing sections, core/README, root README,
  docs/wise/skills-authoring.md porting rules, docs/compatibility.md note.
- C3: update `docs/plans/PLAN-opencode-pi-harness-ports.md` — ports become
  profile + overlay work; versions shift opencode→3.6.0, pi→3.7.0.

### Wave 4 — ship
- D1: version 3.4.0→3.5.0 (plugin.json + codex manifest), full verify
  (`just check` + regen check + unset-env smoke), PR, CI green, merge.

## Verification checklist
1. `python3 scripts/build_ports.py --check` — clean on the committed tree.
2. `python3 scripts/validate_repo.py` — green.
3. `python3 -m pytest harnesses/claude/wise/tests -q` — 83 pass.
4. `bash scripts/install_smoke.sh cursor hermes` — OK.
5. Mutation probe: touch a generated port file → `--check` fails; touch a
   core reference → `--check` fails until regenerated.
6. Review normalization diff list (PR body).

## Out of scope
- opencode / pi ports themselves (next PRs, now profile-driven).
- Any engine (`workflows.py`/`engine.py`/`engine.sh`) or `install.sh` change.
- Generating the claude port's `skills/` (it is the source).
