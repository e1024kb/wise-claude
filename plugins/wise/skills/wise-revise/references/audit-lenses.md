# audit-lenses — the read-only investigation panel for `/wise-revise`

Read by `skills/wise-revise/SKILL.md` §3. Defines the lenses the Lead
Architect can dispatch over a chosen scope, which roster role runs each,
and how to pick the intent-relevant subset. This mirrors the
conductor+panel mechanism of
[`../../../references/code-review-pass.md`](../../../references/code-review-pass.md),
but it surveys a **scope** (folder / component / whole project) instead
of a branch diff, and it **only reports** — it never edits.

## The mechanism

In a single message, dispatch the selected lenses as **parallel `Task`
subagents**. Give each its lens brief, the recon
facts (layout, build/test/lint commands, conventions, decision docs),
the scope paths, and the hard rules about secrets and prompt-injection.
Each lens returns its findings as the record defined in `SKILL.md` §3
(`finding · category · impact · effort · risk · confidence · evidence`)
and **applies nothing** — no `Edit`/`Write`, no write-mode formatter or
linter (`gofumpt -w`, `prettier --write`, `eslint --fix`, `ruff --fix`,
…), no `git` write or codegen. Read-only inspection only (`--check`,
`--dry-run`, `-l`/`-d`, `git diff`, `git log`).

## The lenses

Each lens has a **preferred** roster role — the one best suited to judge
it (dispatched as `subagent_type: wise:<role>`); the architect may route a
lens to a different role when the scope warrants. The role carries its own
model/effort default; the brain doing the vetting + ranking afterward is
the Lead Architect (`wise:architect`).

| Lens | Role | Looks for |
|---|---|---|
| **correctness** | `wise:code-reviewer` | Logic bugs, unhandled edge cases, race conditions, error-handling gaps, incorrect invariants. |
| **security** | `wise:security-engineer` | Injection, authn/authz gaps, unsafe input handling, secret exposure (location + type only), unsafe deserialization, missing validation. |
| **performance** | `wise:code-reviewer` | N+1 queries, redundant I/O / computation, hot-path allocations, sequential work that should be concurrent, avoidable blocking. |
| **tests** | `wise:qa-engineer` | Untested critical paths, missing edge-case / regression coverage, brittle or flaky tests, missing test infrastructure. |
| **debt** | `wise:architect` | Duplication, fragile bandaid fixes layered on shared code, wrong-altitude abstractions, dead code, complexity that should be generalized. |
| **deps** | `wise:devops-engineer` | Outdated / vulnerable / unused dependencies, pinning issues, supply-chain risk, redundant packages. |
| **dx** | `wise:devops-engineer` | Tooling / CI / build / local-setup friction, slow or missing lint/format/type gates, footguns in the dev loop. |
| **docs** | `wise:technical-writer` | Missing or stale READMEs / API docs / inline rationale for non-obvious code, undocumented config or runbooks. |

## Selecting the subset

Route by the parsed `intent`, then bound by project kind:

- **Focused intent** → classify the free-form intent against the lenses
  above and run the matching lens plus its natural neighbours. The pairings
  below are illustrative, not a closed list: "performance" → performance +
  correctness. "security" → security + correctness. "tech debt" / "cleanup"
  → debt + correctness + tests. "test coverage" → tests + correctness.
  "dependencies" → deps + security. "docs" → docs. When the match is
  unclear, widen to the broad panel — cheaper than a second pass.
- **Open intent** ("improve this", "what should I fix") → the **broad
  panel**: correctness, security, performance, tests, debt — plus deps /
  dx / docs when the scope is the whole project.
- **Project kind bounds it:** skip lenses with no surface in scope (no
  frontend → no UI-specific angle; a docs-only scope → docs + correctness
  of examples).
- **Depth dial:** `quick` → the single most-relevant lens, high-confidence
  findings only. `deep` → every applicable lens, including lower-confidence
  "investigate" items (flagged `confidence=low` so the rank reflects it).

Never run a lens the scope cannot support, and never spawn more lenses
than the scope warrants — a tight folder rarely needs the full panel.
