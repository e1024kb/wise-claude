# PLAN-002 — Bootstrap CI + repo validation harness

## Source
- Scope: whole repo (no `.github/` exists)
- Found by: dx + tests + debt lenses (deduped: no-CI, unenforced CONTRIBUTING gates, manifest/skill/workflow validation, version-bump discipline, SHA-pin convention, gitignore hygiene) · leverage 1.0 (impact 5 ÷ effort 5 × confidence 1.0)
- Commit: e9971c5
- Evidence: `.github/` absent; CONTRIBUTING.md:534-548 (manual gate commands); CONTRIBUTING.md:818-820 (version-bump rule; violated by commit 527985e which changed 2 workflows + 2 skills with no plugin.json bump); plugins/wise/CLAUDE.md mirror invariants; .claude-plugin/marketplace.json:15; .gitignore (no `__pycache__/`)

## Summary
The repo documents its own quality gates (JSON manifest parse, `bash -n`,
`py_compile`, stale-name grep, README↔workflow sync, version bumps) but
has no CI, so every invariant relies on contributor discipline — and has
already been violated (PR #21 shipped workflow changes with no version
bump). Done = a GitHub Actions workflow runs the documented gates plus a
new repo-validation script and pytest on every PR/push to main.

## Assumptions
- GitHub Actions is the CI platform (repo is hosted at
  github.com/e1024kb/wise-claude).
- Runtime deps for the scripts: `pyyaml`, `python-ulid`,
  `typing_extensions` (see plugins/wise/scripts/bootstrap-deps.sh:104-128);
  CI installs those + `pytest` via pip.
- `plugins/wise/tests/` may already exist if PLAN-001 landed first; the
  pytest step must pass when the directory is missing or empty
  (`pytest ... || [ $? -eq 5 ]` guard, exit 5 = no tests collected).
- Validation logic can import constants from workflows.py
  (`STEP_TYPES`, `TRIGGER_RULES`, `_parse_frontmatter` at
  workflows.py:124-161, 1007-1026) instead of duplicating them.

## Decisions Made
- One workflow file, `.github/workflows/ci.yaml`, one job, ubuntu-latest,
  Python 3.11+ — this repo ships prose + stdlib-ish scripts; matrix
  builds are overkill.
- New validator lives at `scripts/validate_repo.py` (repo root
  `scripts/`, NOT `plugins/wise/scripts/` — it validates the repo, it is
  not shipped plugin surface).
- Lint steps (`shellcheck`, `ruff check`) run **non-blocking**
  (`continue-on-error: true`) in this plan — the first run may surface a
  backlog; making them blocking is a follow-up once clean.
- Version-bump check: if `git diff --name-only origin/main...HEAD`
  touches `plugins/wise/` (excluding `*.md` at plugin root) but not
  `plugins/wise/.claude-plugin/plugin.json`, fail with a pointer to
  CONTRIBUTING §9. Runs only on pull_request events.
- Do NOT add process-tickets↔process-plans mirror diffing here — noted
  in the index as future work; it needs a normalization spec first.

## Current state
- `.github/` does not exist.
- CONTRIBUTING.md:534-548 lists exactly these manual commands:
  `python3 -m json.tool` over both manifests, `bash -n
  plugins/wise/scripts/*.sh`, `python3 -m py_compile
  plugins/wise/scripts/*.py`, `grep -Rn "/wise:old-name"`.
- Repo invariants documented but unchecked (plugins/wise/CLAUDE.md):
  skill dir name == SKILL.md frontmatter `name`; workflow.yaml `name:`
  == folder name; step `type` ∈ STEP_TYPES; `trigger-rule` ∈
  TRIGGER_RULES; `depends_on` ids exist; `${CLAUDE_PLUGIN_ROOT}/...`
  paths referenced from skills/prompts exist on disk.
- .gitignore has 3 entries (`.DS_Store`, `/.idea`, `/.remember/`) — no
  `__pycache__/` (only plugins/wise/.gitignore covers the literal
  `scripts/__pycache__/` path).

## Tasks
Wave 1
- [ ] Write `scripts/validate_repo.py`: (a) json-parse
      .claude-plugin/marketplace.json + plugins/wise/.claude-plugin/plugin.json;
      (b) yaml-parse all plugins/wise/workflows/*/workflow.yaml and check
      folder-name==`name:`, step ids unique + matching
      `^[a-z][a-z0-9_-]*$`, `type` ∈ STEP_TYPES, `trigger-rule` ∈
      TRIGGER_RULES, `depends_on` targets exist; (c) for every
      plugins/wise/skills/*/SKILL.md check frontmatter parses and `name:`
      == dir name; (d) grep all skills/prompts/references for
      `${CLAUDE_PLUGIN_ROOT}/<path>` and `{{workflow.dir}}/prompts/<file>`
      references and assert the files exist; (e) assert every
      marketplace.json plugin `source` is a local `./` path OR carries a
      40-char commit SHA (repo convention, CLAUDE.md). Import constants
      from plugins/wise/scripts/workflows.py via importlib — New:
      scripts/validate_repo.py — 3 SP
- [ ] Add `__pycache__/` and `*.pyc` to root .gitignore — Reuse:
      .gitignore — 0.5 SP

Wave 2
- [ ] Write `.github/workflows/ci.yaml`: on push to main + pull_request;
      steps = checkout; setup-python; `pip install pyyaml python-ulid
      typing_extensions pytest`; the four CONTRIBUTING §6.2 gate commands
      verbatim; `python3 scripts/validate_repo.py`;
      `python3 -m pytest plugins/wise/tests -q` (tolerate exit 5);
      non-blocking `shellcheck plugins/wise/scripts/*.sh
      plugins/wise/hooks/*.sh` and `ruff check plugins/wise/scripts`;
      version-bump diff check (pull_request only, per Decisions) — New:
      .github/workflows/ci.yaml — 2 SP

Total: 5 SP

## Testing
The harness IS the test. Local dry-run of each gate is the test for the
workflow file (see Validation). `scripts/validate_repo.py` must exit 0
on the current tree at e9971c5 (the correctness lens verified all
cross-references currently resolve — a non-zero exit means the validator
has a bug, not the repo).

## Validation
- `python3 scripts/validate_repo.py` → exits 0, prints per-section OK summary
- `python3 -m json.tool .claude-plugin/marketplace.json > /dev/null && python3 -m json.tool plugins/wise/.claude-plugin/plugin.json > /dev/null` → exits 0
- `bash -n plugins/wise/scripts/*.sh plugins/wise/hooks/*.sh` → exits 0
- `python3 -m py_compile plugins/wise/scripts/*.py scripts/validate_repo.py` → exits 0
- Sabotage check: temporarily set a workflow step `type: bogus` → `python3 scripts/validate_repo.py` exits non-zero naming the file; revert
- `yamllint .github/workflows/ci.yaml` or `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yaml'))"` → exits 0

## Stop conditions
- If `.github/workflows/` already exists at HEAD, halt and report
  (someone added CI since e9971c5 — reconcile instead of overwrite).
- If `scripts/validate_repo.py` fails on the CURRENT tree for a reason
  that looks like a real repo defect rather than a validator bug, report
  the defect; do not "fix" repo content to make the validator pass —
  that is out of scope.
- Do not make shellcheck/ruff blocking in this plan.
