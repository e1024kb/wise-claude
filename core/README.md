# core/ — the canonical harness-neutral source

`core/` holds the harness-neutral source of truth for the `wise` copilot:
the shared prose routines, the workflow engine, the workflow definitions,
and the neutral SDLC agent role cards. **It is not installable itself** —
users install a *port* under `harnesses/<harness>/wise/`, each of which
vendors a copy of the `core/` assets it needs.

```
core/
├── references/     # shared prose routines (grill, pr, commit, supervise, …)
├── agents/         # 13 neutral SDLC role cards (name + description + persona;
│                   #   no tools/model/effort/color frontmatter)
├── workflows/      # the 4 bundled workflow definitions (workflow.yaml + prompts/)
├── scripts/        # the workflow engine: workflows.py, engine.py, engine.sh
└── core-map.yaml   # maps each core asset → its vendored copy per harness
```

## How it's used

Each harness port vendors from here. Some copies are **verbatim** (the
engine, the neutral agent cards on non-Claude ports); others are
**adapted** (the Claude roster adds frontmatter; the non-Claude ports
rewrite `${CLAUDE_PLUGIN_ROOT}` → `${WISE_PLUGIN_ROOT}` and drop skill
frontmatter). `core-map.yaml` records which is which.

## The editing rule

**Edit `core/` first**, then propagate the change into each affected
`harnesses/<harness>/wise/` port by hand — ports are maintained by hand on
purpose, so a change may need harness-specific adaptation. Then run the
drift report to see what diverged:

```
python3 scripts/report_core_drift.py     # or: just drift
```

It byte-diffs every `mode: verbatim` mapping and lists `mode: adapted`
ones as "manually verify". It is **advisory and always exits 0** — drift
is a prompt to review, never a merge gate.

The full sync checklist, the tier model (full / adapted / Claude-only),
and the versioning rules live in the root
[`CONTRIBUTING.md` §10](../CONTRIBUTING.md#10-cross-harness-ports--core-sync).
The neutral agent cards here are catalogued in the Claude port's
[`AGENTS.md`](../harnesses/claude/wise/AGENTS.md).
