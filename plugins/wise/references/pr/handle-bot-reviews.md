# handle-bot-reviews — per-item wizard for ONE review-bot queue

Filtered procedure for walking the user through actionable review
comments from a **single** review bot — Copilot or CodeRabbit
(whichever the caller selects via `bot_filter`). `watch-pipelines.md`
§4 calls this fragment twice, once per bot, so each queue is its
own mini-pipeline with its own top-level gate.

Both bots leave line-level code-anchored review comments via the
same GitHub surfaces; they differ in author login and in the
shape of their `suggestion` blocks. Filtering keeps the wizard's
mental model clean ("now we're in the Copilot queue") and lets
the user skip one bot's queue without losing the other.

## Why a separate fragment (background)

GitHub splits PR comments across three API surfaces:

1. `gh pr view <n> --json comments` — **issue comments**
   (top-level conversation; where bot *summaries* post —
   CodeRabbit's "skip notice", SonarCloud's quality-gate block).
2. `gh api repos/:owner/:repo/pulls/<n>/comments` — **line-level
   review comments** (tied to a `path` + `line`; where Copilot
   and CodeRabbit drop their actionable suggestions).
3. `gh api repos/:owner/:repo/pulls/<n>/reviews` — **reviews**
   (the top-level review object with a `state`, grouping the
   line-level comments).

Pre-0.53 the watch loop only read #1 (summaries) and silently
dropped #2/#3 (the actionable items). This fragment reads all
three and processes only items matching `bot_filter`.

## Context the caller supplies

- `pr_number` — PR number.
- `pr_url` — PR url (for links in the user messages).
- `current_branch` — PR's head branch (for push after fixes).
- `project.path` — absolute path to the repo working tree.
- `bot_filter` — **required**. One of `copilot` or `coderabbit`.
  Matches the GitHub login patterns (`copilot-pull-request-reviewer`,
  `Copilot`, or `coderabbitai`). Anything else → fragment errors
  with `BOT-REVIEWS: error reason=unknown-bot-filter` and returns.
- `bot_display_name` — **required**. Human-readable name shown in
  chat headers (`Copilot` or `CodeRabbit`). Just for UI.

## Procedure

Run all `gh` / `git` commands with `cd <project.path>` first.

### 1. Fetch the three comment surfaces

```bash
PR=<pr_number>
OWNER_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/wise-pr-XXXXXX")"

# Issue comments (top-level). Author/body/url etc.
gh pr view "$PR" --json comments \
  > "$SCRATCH/pr-$PR-issue-comments.json"

# Line-level review comments (path + line + suggestion bodies).
gh api "repos/$OWNER_REPO/pulls/$PR/comments?per_page=100" --paginate \
  > "$SCRATCH/pr-$PR-review-comments.json"

# Review summaries (state: CHANGES_REQUESTED / APPROVED / COMMENTED).
gh api "repos/$OWNER_REPO/pulls/$PR/reviews?per_page=100" --paginate \
  > "$SCRATCH/pr-$PR-reviews.json"
```

Also fetch review threads via GraphQL — we'll need thread IDs for
the Dismiss path later:

```bash
gh api graphql -f query='
  query($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        reviewThreads(first: 100) {
          nodes {
            id
            isResolved
            isOutdated
            comments(first: 1) { nodes { databaseId } }
          }
        }
      }
    }
  }
' -f owner="${OWNER_REPO%/*}" -f repo="${OWNER_REPO#*/}" -F number=$PR \
  > "$SCRATCH/pr-$PR-threads.json"
```

The resulting mapping: each review-comment `databaseId` (from #2)
maps to a thread `id` (GraphQL node ID) which the Dismiss path
resolves. `isResolved` tells us the user already marked the
thread resolved — skip those. `isOutdated` tells us GitHub
marked the anchor stale because the referenced lines moved or
were deleted since the comment was posted — skip those too; the
advice may no longer apply to the current diff, and GitHub itself
renders these with an "Outdated" badge.

### 2. Classify — filtered by `bot_filter`

Walk the combined set. An item enters THIS queue when:

- It's a **line-level review comment** (file #2) with `path` +
  `line`, AND
- Its author login matches the `bot_filter`:
  - `copilot` → `Copilot` OR `copilot-pull-request-reviewer`
    OR login starting with `copilot-` (some orgs use variants
    like `copilot-pull-request-reviewer[bot]`)
  - `coderabbit` → `coderabbitai` OR `coderabbitai[bot]`
- AND its thread is NOT `isResolved: true` (user already marked it resolved),
- AND its thread is NOT `isOutdated: true` (GitHub marked the anchor stale — the referenced lines moved or were deleted since the comment was posted).

OR — it's a **review with state `CHANGES_REQUESTED`** from a
matching bot author, even if the review has no line-level
children (the top-level `body` counts as one actionable item).

**Skip / ignore for THIS queue:**
- Items from any OTHER bot (handled in their own queue).
- Items from humans (handled in `handle-human-comments.md`'s
  queue).
- Issue comments from this bot — those are summaries, not
  suggestions; the watch loop's poll already surfaces them
  elsewhere.
- `APPROVED` / `COMMENTED` reviews without line-level children
  — pure summaries.
- Already-resolved threads (`isResolved: true`).
- Outdated threads (`isOutdated: true`) — GitHub flags these
  with an "Outdated" badge; the code they anchor to has moved,
  so the comment is stale by construction. The bot will re-post
  if the concern still applies after the rebase.

### 3. Top-level gate

If the classified list is empty → announce
`<bot_display_name>: 0 actionable items ✓` in chat and emit
`BOT-REVIEWS: all-clear bot=<bot_filter>`. Skip §4–§7 and emit at §8.

Otherwise, `AskUserQuestion`:

- question: `<bot_display_name>: <K> actionable review comment(s). How do you want to handle them?`
- header: `<bot_display_name>`   (≤12 chars — truncate if needed)
- multiSelect: false
- options:
  - `Paged-bulk (5/page, auto-classified) — recommended` —
    drive collection through `paged-bulk-mode.md`: 5 items on
    screen per page with Claude's pre-classified decision for
    each, the user confirms or edits the whole page in one
    prompt. The fragment only records decisions — the handler's
    apply phases (§5–§8) edit files, commit, resolve threads,
    and push. Pick this as the default — it trades per-item
    walking for a one-click-per-page cadence while keeping
    every decision visible. See §3b.
  - `Fix all in one shot` — Claude synthesises a Fix decision
    for every item in this queue (using the best available
    instruction in §5: suggestion blocks verbatim when present,
    CodeRabbit's `Prompt for AI Agents` block when available,
    otherwise the comment body). Apply phases run as if the
    user had typed `F` for every item via paged-bulk. Pick this
    when you trust the bot's judgment and want the whole queue
    resolved without per-item clicks — typical for CodeRabbit
    where most items are mechanical.
  - `Walk step-by-step` — per-item wizard (§4). Each item gets
    Fix / Fix using suggestion / Dismiss / Skip. Wizard records
    decisions only; the apply phases run after the walk
    finishes. Pick this when you want to inspect every change.
  - `Skip queue — handle later` — don't touch this queue in
    this run. Emit `BOT-REVIEWS: partial bot=<bot_filter> pending=<K>`
    and return.

### 3a. Fix all in one shot

Synthesise a `decisions[]` list with `letter: F` for every item
in the classified list (no UI step). Then jump straight into
§5 — the apply phases handle the actual file edits, commit,
thread resolves, and push, treating the synthesised list as if
it had come from paged-bulk. The §5 Fix path picks the best
available instruction per item (suggestion block verbatim →
CodeRabbit `Prompt for AI Agents` block → comment body) so a
single Fix-all run still benefits from the same per-item
heuristics the Walk wizard would.

If §5 hits an apply-time failure on any item, the queue aborts
with `BOT-REVIEWS: aborted bot=<bot_filter> reason=apply-failed-on=<file:line>` —
the user can re-run and pick `Walk step-by-step` to take items
one at a time.

### 3b. Paged-bulk (5 items / page, auto-classified)

When the user picks `Paged-bulk`, delegate **collection** to
`paged-bulk-mode.md` — the fragment is collect-only and never
runs apply routines:

```text
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/paged-bulk-mode.md
```

Pass the queue-specific inputs:

- `items` — the classified list from §2, each entry carrying:
  - `file:line` — `<path>:<line>` from the review comment
    payload.
  - `link` — **REQUIRED**. Build it explicitly:
    `https://github.com/<OWNER_REPO>/pull/<PR>/changes#r<comment.id>`
    where `<comment.id>` is the numeric `id` field from
    `gh api pulls/:n/comments` (NOT the GraphQL node id, NOT
    the comment's `html_url` — `html_url` resolves to the
    Conversation view, the wrong surface for code-review
    context). The user clicks this row to land on the diff
    with the comment expanded; if it's missing the page is
    useless for a real PR review.
  - `excerpt` — first ~120 chars of the comment body, wrapped.
  - The full comment payload the apply phases need (body,
    `suggestion` block, `Prompt for AI Agents` details block,
    thread id).
- `queue_label = <bot_display_name>` (`Copilot` / `CodeRabbit`).
- `allowed_letters = F,A,D,S`. Meanings — `F` = Fix (Claude
  patches per the chosen instruction); `A` = Fix using
  suggestion (apply suggestion block verbatim — reject when
  the item has no suggestion block, re-ask); `D` = Dismiss
  (resolve thread, no code change); `S` = Skip.
- `auto_classify = true`. Pre-classification heuristics in
  `paged-bulk-mode.md` §2 (bot branch) — CodeRabbit's
  `Prompt for AI Agents` block steers toward `F`; short
  suggestion blocks steer toward `A`; ambiguous items toward
  `S`.
- `picks_action_label = Fix`. The first option of each page
  reads `Fix: <decisions-string>` (e.g. `Fix: 1A 2F 3S 4F 5A`)
  — bot review queues use "Fix" because every positive
  letter on a comment ultimately produces a fix; the
  `Accept` wording is reserved for the Sonar queue (where
  adding a NOSONAR-style suppression is a distinct semantic).
- `decisions` — the output list the fragment populates.

When the fragment returns, `decisions[]` is populated for the
whole queue. Continue into §5 — the apply phases run once for
the full list.

### 4. Walk step-by-step — per-item collection wizard

AskUserQuestion has a 4-option cap. For each item, show up to
four: `Fix (Claude patches)`, `Fix using suggestion` (only when
a `suggestion` block is present), `Dismiss (mark resolved)`,
`Skip — decide later`. When no suggestion block exists, the
`Fix using suggestion` option is omitted (3 options, fine). The
wizard records decisions; **no apply happens here** — the apply
phases (§5–§8) run after the walk finishes.

Per item, compose a preamble Claude prints in chat. The **Link
is REQUIRED** and points at GitHub's Files-changed view so the
user lands on the diff with the comment expanded — that's where
the broader PR context lives. Construct it explicitly (do NOT
use the comment's `html_url` field — that resolves to the
Conversation view `/pull/<n>#discussion_r<id>`, which is the
wrong surface for code-review context):

```
https://github.com/<OWNER_REPO>/pull/<PR>/changes#r<comment.id>
```

`/changes#r<id>` and `/files#r<id>` both work — `/changes` is the
older URL form GitHub still redirects; use it because the
user's terminal-click lands more reliably on it. The `<comment.id>`
is the numeric `id` field from `gh api pulls/:n/comments` (e.g.
`3113566302`), NOT a node-id or GraphQL id.

```
Bot review #<i> of <K>:
  Author:   <login>
  File:     <path>:<line>
  Link:     https://github.com/<OWNER_REPO>/pull/<PR>/changes#r<comment.id>
  Comment:  <first ~150 chars of body, wrapped>
  <if suggestion block present:>
  Suggested:
    <contents of the ```suggestion block, ≤10 lines>
  <if CodeRabbit + "Prompt for AI Agents" <details> block present:>
  Agent-prompt: CodeRabbit shipped a purpose-built prompt; the
                Fix path will drive off it instead of the human-
                facing comment above.
```

Then `AskUserQuestion`:

- question: `Bot review on <path>:<line> — how to handle?`
- header: `Review #<i>/<K>`
- multiSelect: false
- options:
  - `Fix (Claude patches)` — `Record a Fix decision for this comment. The apply phase reads the comment + the file (or CodeRabbit's "Prompt for AI Agents" block when present) and applies a focused edit, then stages it.`
  - `Fix using suggestion` — (only when a suggestion block is present) — `Record a decision to apply the bot's suggestion block verbatim at <path>:<line>. Same end state as clicking "Commit suggestion" in GitHub's UI; staged in the apply phase.`
  - `Dismiss (mark resolved)` — `Record a Dismiss decision. The apply phase resolves the review thread on GitHub without touching code. Use when the comment is outdated / addressed / a false positive.`
  - `Skip — decide later` — `Record a Skip. The comment stays unresolved and shows up in the final summary as pending.`

For each item, append the chosen `(item, letter)` to the queue's
`decisions[]` list:

- `Fix (Claude patches)` → `{ item, letter: F }`
- `Fix using suggestion` → `{ item, letter: A }`
- `Dismiss (mark resolved)` → `{ item, letter: D }`
- `Skip — decide later` → `{ item, letter: S }`

After the last item, fall through to §5. No apply happens
inside the walk loop.

### 5. Phase B — Apply local edits and commit (no push)

`decisions[]` now holds the full queue (from §3a Fix-all, §3b
paged-bulk, or §4 Walk). Walk it in order; per item:

- **`F` — Fix (Claude patches).** Pick the best available
  instruction for the edit, in this order:
  1. If `bot_filter=coderabbit` AND the body contains a
     `<details>` section whose `<summary>` contains the literal
     text `Prompt for AI Agents` (with or without the 🤖
     emoji): extract the fenced code block inside that
     `<details>` and treat its contents as the primary fix
     instruction. CodeRabbit ships this block specifically so
     agents drive the fix off a focused directive rather than
     the human-prose explanation above it. Copilot doesn't
     emit this block, so for the Copilot queue this branch is
     never taken.
  2. Otherwise, use the comment body verbatim.

  Read the referenced file + the chosen instruction + any
  surrounding file context Claude needs. Apply a focused edit —
  do NOT touch code outside the review's scope. Then:

  ```bash
  git add -- "<path>"
  ```

  Append this item's review-thread id (looked up from
  `"$SCRATCH/pr-$PR-threads.json"` by matching the review-comment's
  numeric `id` against `comments.nodes[0].databaseId`) into
  `FIXED_THREAD_IDS` for the §6 resolve.

- **`A` — Fix using suggestion.** Parse the comment body for the
  `suggestion` block:

  ```
  \`\`\`suggestion
  <new code>
  \`\`\`
  ```

  Apply it to `<path>` at `<line>` (or `<start_line>..<line>`
  for multi-line). Use the `Edit` tool to replace exactly those
  lines with the suggestion body. GitHub's native "Commit
  suggestion" button does the same thing server-side; we do it
  locally because the REST API doesn't expose a direct "apply
  suggestion" endpoint, and doing it via git gives us the full
  commit message convention from `commit-from-fix.md`. Stage as
  the `F` path and append the thread id to `FIXED_THREAD_IDS`.

- **`D` — Dismiss.** No local action. Append the thread id to
  `DISMISS_THREAD_IDS` for the §6 resolve. (Fix and Fix-using-
  suggestion already auto-resolve in §6 too — Dismiss is the
  same operation, just without a code change.)

- **`S` — Skip.** No local action. Append the item's
  `<file:line>` to `pending_items` for §8.

**Apply-time failure mode.** If a routine throws (malformed
suggestion, conflicting edit, file vanished since fetch, etc.):
fail fast. Print one line in chat naming items already applied
plus the failing item's `<file:line>`. Do **not** commit, do
**not** run §6, do **not** push. Emit at §8:

```
BOT-REVIEWS: aborted bot=<bot_filter> reason=apply-failed-on=<file:line>
```

The handler returns and lets the user re-run the queue (the
Walk wizard or paged-bulk picks up where they left off — bot
threads that were already resolved get filtered in §1 next
time).

If at least one staged change exists after the walk, drive
`commit-from-fix.md` with `push=no`,
`fix_kind=review-comments`, and `fix_summary="applied <K>
<bot_display_name> review comment(s)"`. Expect:

- `COMMIT: ok subject="…" pushed=no` → continue to §6.
- `COMMIT: skip` → no Fix / Accept landed (only Dismiss + Skip);
  skip §6's Fix/Accept resolves and continue at §6's Dismiss
  block.
- `COMMIT: failed` → emit
  `BOT-REVIEWS: aborted bot=<bot_filter> reason=commit-failed`.

### 6. Phase C — Apply remote side effects

Resolve every thread the user signalled "addressed" via Fix /
Fix-using-suggestion / Dismiss. Loop over
`FIXED_THREAD_IDS ∪ DISMISS_THREAD_IDS`:

```bash
RESOLVED=0
for THREAD_ID in "${FIXED_THREAD_IDS[@]}" "${DISMISS_THREAD_IDS[@]}"; do
  if gh api graphql -f query='
    mutation($threadId: ID!) {
      resolveReviewThread(input: { threadId: $threadId }) {
        thread { isResolved }
      }
    }
  ' -F threadId="$THREAD_ID" >/dev/null 2>&1; then
    RESOLVED=$((RESOLVED + 1))
  fi
done
```

Failures (403, thread already resolved by someone else, no
write access) log + continue — the fix itself landed in §5; the
bot will re-flag on the next pass if the concern still applies.
Record the successful resolve count as `<R>` for §8.

### 7. Phase D — Push

If §5 produced a commit (`COMMIT: ok pushed=no`), run a single
push now:

```bash
git push
```

On failure (non-fast-forward, auth, hook), do NOT retry, do
NOT force-push. Emit
`BOT-REVIEWS: aborted bot=<bot_filter> reason=push-failed`.

On success, re-enter `watch-pipelines.md §1` — the push may
kick new CI runs AND new review bot passes; we want them
captured in the next iteration.

If §5 emitted `COMMIT: skip` (all Dismiss + Skip — no commit
landed), there's nothing to push. Skip directly to §8 without
re-polling — Dismiss already resolved its threads in §6, Skip
opts out.

### 8. Emit the final line

The caller (`watch-pipelines.md`) consumes this line. `bot=` is
the `bot_filter` value the caller supplied (`copilot` or
`coderabbit`) so two invocations of this fragment can be told
apart in the caller's roll-up:

```
BOT-REVIEWS: all-clear bot=<bot_filter>                                                  # 0 items
BOT-REVIEWS: handled bot=<bot_filter> committed=<count> resolved=<count>                 # all applied + pushed + threads resolved
BOT-REVIEWS: partial bot=<bot_filter> pending=<item1,item2,…>                            # user skipped items (queue-skip or per-item skip)
BOT-REVIEWS: aborted bot=<bot_filter> reason=<short-reason>                              # push failure or abort
```

`committed=<count>` is the number of items that ended up in the
queue's local commit (Fix + Fix-using-suggestion). `resolved=<count>`
is the number of review threads §6 successfully resolved
(`FIXED_THREAD_IDS ∪ DISMISS_THREAD_IDS`). Equal to
`committed + dismissed` when every resolve succeeded; lower when
some threads couldn't be resolved (403, already resolved, no
write access) — the mismatch is expected and non-fatal.

`reason=` on the `aborted` line is one of:

- `apply-failed-on=<file:line>` — Phase B (§5) hit a routine
  error on that item; nothing was committed.
- `commit-failed` — `commit-from-fix.md` returned `COMMIT: failed`
  (typically a pre-commit hook).
- `push-failed` — Phase D (§7) `git push` rejected the push
  (non-fast-forward, auth, hook).

Examples:

```
BOT-REVIEWS: all-clear bot=copilot
BOT-REVIEWS: handled bot=coderabbit committed=5 resolved=5
BOT-REVIEWS: handled bot=copilot committed=3 resolved=2
BOT-REVIEWS: partial bot=copilot pending=AuditPanel.tsx:42,SectionQuery.php:101
BOT-REVIEWS: aborted bot=coderabbit reason=apply-failed-on=AuditPanel.tsx:88
```

## Guardrails

- Never apply a suggestion outside the lines it targets. The
  suggestion block is a literal replacement for the exact `line`
  (or `start_line..line`) range — don't drift into surrounding
  code.
- Only resolve a thread when the user explicitly acted on it —
  Fix, Fix-using-suggestion, or Dismiss all qualify (each one is
  the user's signal that the comment is addressed, whether by
  code change or by explicit dismissal). Skip does NOT qualify
  — a skipped thread stays unresolved so the pending list in §8
  reflects what's genuinely still open. Never resolve a thread
  outside those three user actions. If a resolve call fails
  (403, already resolved, no write access), log and continue —
  the user's intent was captured; the GitHub-side state is
  best-effort.
- **Never push between phases.** Phase B uses `commit-from-fix.md`
  with `push=no`; Phase D is the single place a `git push`
  happens. Pushing inside Phase B would leak commits onto the PR
  before §6's thread resolves and undo the back-and-forth fix
  the queue exists to deliver.
- Never respond inline on GitHub to bot review comments in this
  fragment. Reply-on-GitHub is a human-queue behaviour (see
  `handle-human-comments.md`).
- Process only items matching `bot_filter`. Items from the other
  bot get their own invocation of this fragment — don't
  cross-process.
- Stop after 10 wizard rounds in a single `watch-pipelines`
  invocation (safety catch — if we're still processing bot
  comments after 10 iterations, the PR is in a fight with the
  review bot that auto-posts on every commit; escalate to chat).
- `rm -rf "$SCRATCH"` before EVERY exit — the final line (§8), the
  empty-queue `all-clear` (§2), `Skip queue` (§3), and every early
  `emit … and return` abort (`apply-failed-on` / `commit-failed` in
  §5, `push-failed` in §7). None of them may leave the scratch dir
  behind.
