# core/ — the canonical harness-neutral source

`core/` holds the harness-neutral source of truth for the `wise` copilot:
the shared prose routines, the workflow engine, the workflow definitions,
the neutral SDLC agent role cards, and the port-generator inputs. **It is
not installable itself** — users install a *port* under
`harnesses/<harness>/wise/`, each of which is generated from `core/` (plus
the Claude port's skills) by `scripts/build_ports.py`.

```
core/
├── references/     # shared prose routines (grill, pr, commit, supervise, …)
├── agents/         # 13 neutral SDLC role cards (name + description + persona;
│                   #   no tools/model/effort/color frontmatter)
├── workflows/      # the 4 bundled workflow definitions (workflow.yaml + prompts/)
├── scripts/        # the workflow engine: workflows.py, engine.py, engine.sh
└── ports/          # generator inputs
    ├── profiles/   # one <harness>.yaml per port (tiers, frontmatter rules)
    ├── notes/      # harness-adaptation note templates
    ├── overlays/   # per-skill find/replace hunks for divergent prose
    └── static/     # byte-copied per-port files (README, manifests)
```

## How it's used

`scripts/build_ports.py` renders every port from here. Some outputs are
byte-copies (the engine, the neutral agent cards on non-Claude ports);
others are transformed (the Claude roster gains `tools` / `model` /
`effort` / `color` frontmatter from `profiles/claude.yaml`; port skills
are derived from the Claude skills with `${CLAUDE_PLUGIN_ROOT}` rewritten
per the context-dependent rule in `CONTRIBUTING.md` §10.3 — the defaulted
`${WISE_PLUGIN_ROOT:-…}` expansion in bash contexts, the short
`${WISE_PLUGIN_ROOT}` in prose — plus reduced frontmatter and a
harness-adaptation note). The generated output is committed;
`python3 scripts/build_ports.py --check` (run in CI) fails on any drift.

## The editing rule

**Edit the sources** — `core/` for anything harness-neutral,
`harnesses/claude/wise/skills/` for skills, `core/ports/` for per-port
adaptations — **then regenerate**:

```
python3 scripts/build_ports.py     # or: just build
```

Never hand-edit a generated file; `--check` (or `just build-check`)
verifies the committed tree matches a fresh render and exits non-zero
on any diff.

The full procedure, the tier model (full / adapted / Claude-only), and
the versioning rules live in the root
[`CONTRIBUTING.md` §10](../CONTRIBUTING.md#10-cross-harness-ports--the-port-generator).
The neutral agent cards here are catalogued in the Claude port's
[`AGENTS.md`](../harnesses/claude/wise/AGENTS.md).
