# paged-bulk-details — auto-classify heuristics + decision-string grammar

On-demand companion to `paged-bulk-mode.md`. The gate/flow core stays
in that file; this one holds the two reference tables it consults
mid-run. Read it lazily:

- §A when `auto_classify=true` (before suggesting letters in the
  core's §2);
- §B before parsing a typed decision string (the core's §3c/§3d).

## A. Auto-classify heuristics (core §2)

**Bot-review items (Copilot / CodeRabbit):**

- `Prompt for AI Agents` `<details>` block present (CodeRabbit
  only) → `F`. Rationale: "agent-prompt present — Claude will
  follow it."
- `suggestion` block present AND ≤3 replaced lines → `A`.
  Rationale: "mechanical suggestion — 1-line swap."
- `suggestion` block present AND >3 replaced lines → `F`.
  Rationale: "suggestion touches a block — patch manually for
  clarity." (A suggestion that rewrites many lines is usually a
  structural rewrite the file-level Fix path handles better than
  a verbatim apply.)
- Body reads as a bug / regression claim ("this breaks X",
  "wrong when Y", "null deref", "off-by-one") → `F`. Rationale:
  "bot flagged a functional bug."
- Body is a style / naming nit with no suggestion block → `F`.
  Rationale: "style nit."
- Low confidence / the item reads as a question or opinion →
  `S`. Rationale: "needs human judgement — skipping."
- Never auto-classify `D` — Dismissing a thread is the user's
  explicit call.

**Sonar items:**

- Rule is a mechanical fix that doesn't change behaviour
  (unused import, missing `const`, trailing whitespace, naming
  violation, dead branch) → `F`. Rationale: "mechanical rule."
- Rule is a high-false-positive rule (cognitive complexity,
  nesting depth, `NOSONAR`-appropriate) → `A`. Rationale:
  "false-positive-prone — accept with suppression."
- Rule is a potential bug (`null` deref, off-by-one, lost
  `await`) → `F`. Rationale: "bug rule."
- Rule is ambiguous / needs context → `F`. Rationale: "attempt a
  fix; the apply phase falls back to an Accept (suppression) when a
  patch would change behaviour." The Sonar queue has no `S` — every
  issue must be Fixed or Accepted.

**Humans:** auto-classify is disabled; no suggestion is shown.
Rationale: human review comments carry opinion, domain
knowledge, and review intent that Claude shouldn't pre-grade.
The paged-bulk benefit for humans is the on-screen list + one
free-form prompt — still a time saver without the per-item
suggestion.

## B. Decision-string grammar (core §4)

Normally, tokens are separated by whitespace, comma, or
semicolon. A token is one of:

- `<index>[=:]?<letter>` — explicit index with no internal
  whitespace, e.g. `1F`, `1=F`, `1:F`, `3a`
  (case-insensitive letter).
- `<letter>` alone — positional when provided as separated
  single-letter tokens, e.g. `F A S S F`.

Compact positional form is also allowed: if the entire input is
letters only (case-insensitive), with no indices or separators,
split it into individual characters and treat each character as
a positional `<letter>` token. For example, `FASSF` means
`F A S S F`.

When every token in the string is positional (whether separated
or compact), they map to items 1..N in order. Mixing indexed
and positional forms is not allowed; if at least one token has
an index, every token must.

Letters are case-insensitive (`F`, `f`, `A`, `a`, …).

**Validation — re-ask on any of these:**

| Problem                                     | Re-ask message                                |
|---------------------------------------------|-----------------------------------------------|
| Unknown letter (e.g. `1X`)                   | `Unknown letter "X" — allowed: <allowed_letters>.` |
| Letter not in `allowed_letters` (e.g. `R` on Sonar) | `Decision "R" isn't valid in the <queue_label> queue — allowed: <allowed_letters>.` |
| Index < 1 or > page size (e.g. `7F` on a 5-item page) | `Index 7 is outside this page (1..<K>).`     |
| Duplicate index (e.g. `1F 1A`)              | `Index 1 appears twice — one decision per item.` |
| Mixed indexed + positional                  | `Mix of indexed and positional tokens — pick one style for the whole string.` |
| Positional count > page size                | `Too many decisions — this page has <K> items.` |

**Missing indices** (when some items have no decision) are
**NOT** silent — treat them as `S` AND print a visible note
before applying:

```text
Implicit skip: items <comma-separated indices> — no decision in the input, treated as Skip.
```

This lets the user notice a typo that would otherwise be
invisible. If the note catches them by surprise, they can edit
and re-submit (the prompt re-emits automatically only on parse
errors; implicit skips apply, but the note is loud enough that
the user can always abort the next page via Skip queue).

Examples (assume bot queue, `allowed_letters=F,A,D,S`):

| Input             | Parsed                     |
|-------------------|----------------------------|
| `1F 2A 3D 4S 5F`  | clean — 5 decisions        |
| `FASSF`           | positional — 1F 2A 3S 4S 5F |
| `1F 3A 5D`        | implicit skip on 2, 4      |
| `1F, 2a, 3S`      | clean — commas + case-insensitive |
| `1:F; 2:A; 3:D`   | clean — colon + semicolon  |
| `1F 1A`           | re-ask (duplicate)         |
| `1F 2R` on Sonar  | re-ask (R not allowed)     |
| `7F` on a 5-item page | re-ask (out of range)      |

