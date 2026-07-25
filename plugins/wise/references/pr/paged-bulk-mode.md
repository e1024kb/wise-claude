# paged-bulk-mode — 5-items-per-page bulk triage with auto-classify

Shared fragment for the four reviewable queues
(`handle-human-comments.md`, `handle-bot-reviews.md` × 2, and
`handle-sonar-issues.md`). Each queue's top-level gate offers
`Paged-bulk` as the recommended cadence; when the user picks it,
the handler reads this fragment and drives its procedure with
the queue-specific inputs below.

**Why paged-bulk.** Walking items one AskUserQuestion at a time
is slow on PRs with many findings — most of the wait is
round-trips, not thinking. Paged-bulk puts 5 items on screen at
once with Claude's pre-classified decision for each (auto-
classify), then collects all 5 decisions in a single prompt. The
common case becomes a one-click confirm; the exception case is
editing a 10-character string.

**Collect-only — this fragment never applies decisions itself.**
Page-level apply was the original shape; it produced visible back-
and-forth (each page's `D` ran a GraphQL resolve inline, each `R`
posted via `gh api` inline, file edits + thread-resolves + push
landed in different orders depending on which page they belonged
to). The fragment now **only collects decisions**: every validated
page appends `(item, letter)` entries to a queue-level
`decisions[]` list, and after the last page control returns to
the handler with the full list. The handler's apply phases
(local-edit-and-commit → remote-side-effects → single push) own
every side effect.

For Reply (humans queue) the user's decision DOES include the
draft body — drafting + per-reply confirmation happen during
collection (with one quick `AskUserQuestion` per `R` letter) so
the user reviews each reply's wording before the queue commits to
posting it. The actual `gh api` POST is still deferred to the
handler's remote phase; only the wording is locked in here.

## Context the caller supplies

- `pr_number`, `pr_url`, `project.path` —
  passed through to the per-item apply routines.
- `items` — the classified list the handler already built, in
  the same shape its Walk mode would consume and in the order
  it intends for them to be processed. Each item carries at
  minimum: `file:line` (or "PR top-level" for top-level issue
  comments), `link` (the URL to put in the preamble), `excerpt`
  (≤120 chars of body / message), and any queue-specific
  payload the apply routines need (`suggestion` block,
  `Prompt for AI Agents` block, sonar `rule` / `severity`,
  thread id, …). This fragment derives a `page_index` for each
  item on the current page: **`page_index` is always 1..K**
  (where `K` is the item count on the current page, at most 5)
  and numbering restarts at 1 on every page. The render prints
  `page_index`, the parser validates `page_index`, and the
  dispatch maps `page_index` back to the corresponding item.
  If an item also carries a queue-global `index` (or any other
  queue-global position) from the caller, treat it as metadata
  only — do **not** render it to the user and do **not** use it
  for parsing or dispatch; it would invite inputs like `7F` on
  a 5-item page that the parser must then reject.
- `queue_label` — short label used in chat headers
  (`Copilot`, `CodeRabbit`, `Sonar`, `Humans`).
- `allowed_letters` — the set of decision letters valid for
  this queue. One of:
  - `F,A,D,S` — bot review queue (Copilot / CodeRabbit)
  - `F,A`     — Sonar queue (no Skip — every issue Fixed/Accepted)
  - `F,R,D,S` — humans queue
  Letter meanings (the queue context determines `A`):
  - `F` — Fix (Claude patches per the comment / rule)
  - `A` — for bot review queues: Fix using suggestion (apply
    the bot's `suggestion` block verbatim); for Sonar: Accept
    (add a NOSONAR-style suppression with rationale, or call
    the Sonar MCP `change_issue_status` when available)
  - `D` — Dismiss (resolve the review thread, no code change)
  - `R` — Reply (humans only — draft + confirm wording during
    collection; post in the handler's remote phase)
  - `S` — Skip (record in the pending list)
  Use the queue-specific label (`Fix using suggestion` for
  bots, `Accept (add suppression)` for Sonar) when rendering
  the `A` letter in chat — never just print "Accept" for a
  bot review queue, the rename was deliberate.
- `auto_classify` — `true` for Copilot / CodeRabbit / Sonar;
  `false` for Humans. When false, no pre-classification is
  shown and the picks-action option (see below) is omitted.
- `picks_action_label` — required when `auto_classify=true`.
  The verb used in the first option's label, rendered as
  `<picks_action_label>: <decisions-string>`. Two values are in
  use:
  - `Fix` — bot review queues (Copilot, CodeRabbit). Matches
    the handler-level rename of "Accept" actions to "Fix" for
    PR comments.
  - `Accept` — Sonar queue. Sonar keeps the "Accept" wording
    because adding a NOSONAR-style suppression is genuinely a
    distinct semantic ("we are choosing to live with this rule
    violation"), not a fix; conflating the two would mislead.
  Don't "consistency-clean" this back to a single verb in a
  future edit — the asymmetry is intentional.
- `decisions` (output, mutable list) — the fragment appends
  `(item, letter [, reply_body])` entries here as each page
  validates. The handler reads it after the fragment returns.
  The fragment does **not** invoke `apply_*` routines, file
  edits, GraphQL mutations, `gh api` posts, or commits — every
  side effect happens in the handler's apply phases.

## Procedure

### 1. Chunk the list

Page size is **5**. Chunk `items` into pages of 5 in order
(last page may be shorter). Call the list of pages `PAGES` and
its length `TOT`.

### 2. Auto-classify (when enabled)

For each item in the full list, pick a suggested letter from
`allowed_letters` using the heuristics below — and a one-line
rationale you'll print alongside the suggestion so the user can
judge at a glance. If `auto_classify=false`, skip this section
entirely; items carry no suggestion.

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

### 3. Per-page loop

For each page `P` in `PAGES` (indexed 1..TOT):

#### 3a. Render the page in chat

Print one chat message. **The `Link:` row is REQUIRED on every
item** — the user clicks it to jump to the comment / issue in
its native UI (GitHub Files-changed view for line-level review
comments, GitHub Conversation view for top-level issue
comments, SonarCloud issue page for Sonar). Never collapse it
into the `path:line` row, never drop it from the render. Use
the labelled multi-line shape below — same shape as the
Walk-mode preamble — and do NOT silently restructure into a
denser layout when an item has a long body or many fields:

```text
<queue_label>: page <P> of <TOT> — <K> item(s)

#: 1
  File:     <path>:<line>   (or "PR top-level" for top-level issue comments)
  Link:     <link>
  Item:     <excerpt, ≤120 chars, wrapped>
  <if auto_classify, two extra rows:>
  My pick:  <human-readable label for the suggested letter>
  Reasoning: <one-line rationale from §2>

#: 2
  File:     …
  …

<blank line>
<if auto_classify: "Claude suggests: 1<L> 2<L> 3<L> 4<L> 5<L>">
```

Render rules for the auto-classify rows:

- **`My pick:` uses the queue-specific human label, not the
  raw letter.** For bot review queues: `F` → `Fix`,
  `A` → `Fix using suggestion`, `D` → `Dismiss`, `S` → `Skip`.
  For Sonar: `F` → `Fix`, `A` → `Accept (add suppression)`,
  `S` → `Skip`. For humans: not applicable —
  `auto_classify=false` so these rows are omitted entirely.
  Never print `My pick: Accept suggestion` for a bot review
  queue — the rename was deliberate; the legacy label leaks
  through if you reach for it from training data.
- The `Claude suggests:` summary line at the bottom uses the
  raw letters (`1A 2F 3S …`) — that's the same string the
  user types into Other to override decisions, so the letters
  are the right currency there.

The `link` and `excerpt` come from the `items` list the handler
built — don't re-fetch. For bot / human comments, `link` is the
GitHub `/changes#r<id>` URL (line-level) or `html_url` (top-
level issue comment). For Sonar, it's the SonarCloud
`&open=<issue.key>` URL.

#### 3b. Collect decisions for this page

Emit ONE `AskUserQuestion`:

- question: `<queue_label> page <P>/<TOT>: confirm decisions or edit.`
- header: `<queue_label> <P>/<TOT>`   (≤12 chars — truncate)
- multiSelect: false
- options (in this order; the first ≤4 go in; the picks-action
  option is omitted when `auto_classify=false`):
  1. `<picks_action_label>: <decisions-string>` — only when
     `auto_classify=true`. The label uses the caller's
     `picks_action_label` verb. Examples — `Fix: 1A 2F 3S 4F 5A`
     for bot queues, `Accept: 1A 2F 3S 4F 5A` for Sonar.
     Recommended; picking this records Claude's picks for the
     whole page into `decisions[]` verbatim.
  2. `All Fix` — record every item on this page as `F`.
     Shortcut for pages of obvious patches.
  3. `All Skip` — record every item as `S` and advance to the
     next page without recording any apply-bound decisions.
  4. `Custom decisions` — edit the decisions via the always-
     available **Other** text input. Use this to override
     Claude's picks or to type the string from scratch.

For the Humans queue (`auto_classify=false`), the three visible
options are `All Fix` / `All Skip` / `Custom decisions` — no
picks-action option.

#### 3c. Resolve the chosen string

- `<picks_action_label>: <str>` → use `<str>` verbatim.
- `All Fix` → synthesise `1F 2F 3F 4F 5F` (trimmed to page
  size).
- `All Skip` → synthesise `1S 2S 3S 4S 5S` (trimmed to page
  size).
- `Custom decisions` → the user typed a string via Other. Parse
  it with the grammar in §4.

**Other-text precedence.** `AskUserQuestion` always exposes an
`Other` free-form text input regardless of the option set. Apply
this precedence when resolving the response:

- If the user picked `Custom decisions` → parse the Other text.
- If the user picked `Other` directly (no option label chosen,
  only free-form text) → treat that as `Custom decisions` and
  parse the Other text.
- If the user picked the picks-action option (`Fix:` / `Accept:`)
  / `All Fix` / `All Skip` AND also typed in Other → the
  selected option wins; **ignore the Other text** (do not
  silently merge it). Log a one-line note in chat:
  `"Ignored extra text typed in Other — <option-label> was
  picked. Re-run and pick Custom decisions if you meant to
  override."`.
- If the user picked `Custom decisions` but the Other text is
  empty or whitespace-only → re-ask the same page with
  `"Custom decisions picked but no string typed — type a
  decision string in Other, e.g. '1F 2A 3S 4F 5A' (or pick All
  Skip to skip the whole page)."`. An empty string would
  otherwise parse as "no tokens" and implicit-skip every item
  on the page; force an explicit decision.
- If the user picked nothing AND typed nothing → re-ask the
  same page with `"No decision recorded — pick an option or
  type a decision string in Other."`.

#### 3d. Validate and record

Parse the resolved string (§4). On any parse error, print the
error to chat and re-emit the same AskUserQuestion for this
page — do NOT advance. Do NOT record a partial page.

Before recording, run a **pre-record per-item validation** pass
— §4's grammar check only guards the decision-string itself;
some letters are additionally only valid on items carrying
specific payload:

- `A` on a bot-review item requires a `suggestion` block on
  that item. If missing → re-ask this page with
  `"Item <n>: A requires a suggestion block on the comment —
  this item has none. Use F (Claude patches from the body)
  or S."`
- `D` on a human item requires a resolvable review thread
  (line-level review comment, not a top-level issue comment).
  If the item is top-level → re-ask with
  `"Item <n>: D only works on line-level review threads —
  top-level issue comments can't be resolved. Use R (reply)
  or S."`
- `D` on a bot-review item requires a resolvable review
  thread id on the item. `handle-bot-reviews.md §2` classifies
  top-level `CHANGES_REQUESTED` reviews as actionable items
  even though they have no thread id; those cannot be
  resolved later. If the item has no thread id → re-ask with
  `"Item <n>: D can't apply — this bot review is a top-level
  CHANGES_REQUESTED with no thread to resolve. Use F, A, or
  S."`
- Sonar items need no extra per-item pre-record validation:
  `allowed_letters=F,A,S` already excludes `D`, and every
  Sonar item has a target path so `A` (suppression) always
  applies later.

If every `(item, letter)` pair in the page passes validation,
walk the page in order and append a decision entry to
`decisions[]` per item:

- `F` → append `{ item, letter: F }`.
- `A` → append `{ item, letter: A }`.
- `D` → append `{ item, letter: D }`.
- `R` (humans only) → draft the reply body inline. Print the
  draft in chat (one short block — the comment's body or
  excerpt for context, then `Proposed reply:` followed by the
  draft text). Then `AskUserQuestion`:
    - question: `Reply to @<author> on <file:line> — confirm or edit?`
    - header: `Reply <i>/<N>` (≤12 chars; truncate)
    - options: `Post as drafted` / `Skip — don't reply` —
      the always-available Other input lets the user paste
      an edited reply body verbatim. If the user typed Other,
      use that as the final body; if they picked
      `Post as drafted`, use the drafted body; if they picked
      `Skip — don't reply`, downgrade this item's letter to
      `S` (record `{ item, letter: S }`) and skip the append
      below.
  Then append `{ item, letter: R, reply_body: <final-body> }`.
  Drafting + confirmation is the only network-shaped activity
  this fragment performs; no `gh api` POST happens here — the
  handler's remote phase posts every confirmed reply at once.
- `S` → append `{ item, letter: S }`.

If any pre-record check fails, re-emit the same
`AskUserQuestion` for this page without recording anything.
The page is atomic at pre-record time — either all items pass
validation or the user edits the string and we try again. No
partial page from a validation miss.

**Recording is side-effect-free** for `F`, `A`, `D`, `S` — the
fragment only mutates `decisions[]`. `R` is the one exception:
it shows the draft and asks the user to confirm wording, but
even then nothing posts. The handler's apply phases (file
edits, commit, thread-resolves, replies, push) own every
side effect.

#### 3e. Progress line

After each page, print a one-line progress marker in chat:

```text
<queue_label> page <P>/<TOT> recorded — fix=<aF> accept=<aA> reply=<aR> dismiss=<aD> skip=<aS>
```

The counts reflect what was added to `decisions[]` from this
page. Nothing has been applied yet; the markers tell the user
what the handler will do once collection finishes.

### 4. Decision-string grammar

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

### 5. After the last page — return decisions to the handler

Control returns to the handler with `decisions[]` populated.
The handler runs its phased apply (Phase B local-edit-and-
commit → Phase C remote thread-resolves + replies + Sonar MCP
calls → Phase D single push) and then re-enters
`watch-pipelines §1`. No side effects have happened yet at the
moment this fragment returns.

Emit the handler's usual final verdict line. The verdict uses
the same grammar (`handled committed=<K> resolved=<M>` /
`partial pending=…` / `all-clear` / `aborted reason=…`);
paged-bulk does not introduce a new marker.

## Guardrails

- **Collect-only.** This fragment never edits files, never
  stages, never commits, never resolves threads, never posts
  replies. Every side effect lives in the handler's apply
  phases.
- **No silent drops.** Missing indices become explicit `S` with
  a visible note (§4). Parse errors re-ask the same page
  without advancing.
- **Auto-classify is a suggestion, not an apply.** Claude's
  picks are rendered inline and behind the picks-action option;
  the user always has the final decision via the option set or
  the Other text input.
- **Humans don't get auto-classify.** Matches the existing
  human-queue guardrail ("Never auto-fix a human comment
  without their decision") — paged-bulk surfaces the list and
  the prompt, nothing else.
- **Respect `allowed_letters`.** Reject letters that aren't in
  the queue's set, even when the user types them via Other.
  Sonar rejects `D` and `R`; Humans reject `A`; bot queues
  reject `R`. The re-ask message names the allowed letters.
- **Page size is 5, not configurable in this release.** Hard-
  coded to keep the prompt shape predictable and the picks-
  action label short enough to fit AskUserQuestion's chip.
- **`picks_action_label` is queue-specific and intentional.**
  Bot queues use `Fix:` to match the rest of their wizard
  ("Fix" = positive action on a comment); Sonar uses
  `Accept:` because adding a NOSONAR-style suppression isn't
  a fix, it's an explicit acceptance of a rule violation.
  Don't normalise these to a single verb in a future edit.
