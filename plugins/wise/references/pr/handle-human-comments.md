# handle-human-comments — walk user-left PR comments one by one

One of the four reviewable queues `watch-pipelines.md` §4
dispatches to. Called FIRST, before the bot queues, because human
comments are the most valuable signal and can short-circuit the
rest of the loop (a human asking "please hold off" means stop
autofixing everything else).

## Context the caller supplies

- `pr_number`
- `pr_url`
- `project.path`

Sibling fragments this handler reads (`commit-from-fix.md`,
`paged-bulk-mode.md`) live alongside it in
`${CLAUDE_PLUGIN_ROOT}/references/pr/`.

## Procedure

Run all `gh` commands with `cd <project.path>` first.

### 1. Fetch human comments across all three GitHub surfaces

Three GitHub API endpoints carry PR comments (issue comments,
review comments, reviews). For each, keep only entries whose
author is human. An author is NOT human if **any** of the
following matches — this list MUST stay aligned with the bot
classifiers in `handle-bot-reviews.md` §2, otherwise a Copilot /
CodeRabbit comment leaks into this queue AND the bot queue and
the user walks it twice:

- `.user.type == "Bot"` — GitHub's own flag. Necessary but not
  sufficient: Copilot's built-in reviewer has been observed to
  surface with `type: "User"` on the REST payload, so also
  apply the login checks below.
- login is the exact string `Copilot` — GitHub's built-in
  reviewer posts under this literal login. The bare `copilot-*`
  glob does NOT catch it (the glob needs a trailing hyphen the
  `Copilot` login doesn't have); this line is the one that
  matters.
- login is `copilot-pull-request-reviewer`, OR starts with
  `copilot-` (covers org variants like
  `copilot-pull-request-reviewer[bot]`).
- login is `coderabbitai` or `coderabbitai[bot]`.
- login is `github-actions` or ends with `[bot]`.
- login is `sonarcloud` or `sonarqubecloud`.

```bash
PR=<pr_number>
OWNER_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/wise-pr-XXXXXX")"

gh pr view "$PR" --json comments > "$SCRATCH/wise-hum-issue-$PR.json"
gh api "repos/$OWNER_REPO/pulls/$PR/comments?per_page=100" --paginate > "$SCRATCH/wise-hum-review-$PR.json"
gh api "repos/$OWNER_REPO/pulls/$PR/reviews?per_page=100"  --paginate > "$SCRATCH/wise-hum-reviews-$PR.json"
```

Also pull review-thread resolution + outdated state (same GraphQL
query as `handle-bot-reviews.md` §1 — it fetches both
`isResolved` and `isOutdated`) to skip threads the user already
resolved AND threads GitHub flagged as outdated (the referenced
lines moved or were deleted, so the comment's anchor is stale).

Merge into a flat list; keep only items from human authors whose
thread is neither `isResolved: true` nor `isOutdated: true`. Drop
empty-body review-summary entries whose children are already
covered.

### 2. If empty, announce and exit

No items → print `Humans: 0 new comments ✓` in chat and emit the
final line `HUMANS: all-clear`. Skip §3–§7 and emit at §8.

### 3. Top-level gate

Humans say qualitative things (questions, suggestions,
refactoring proposals, "LGTM" notes) — "fix all in one shot"
doesn't apply. Offer three options: paged-bulk (the default),
walk step-by-step, or skip. The wizard records decisions; the
handler's apply phases (§5–§8) run after collection finishes.

`AskUserQuestion`:

- question: `<N> human comment(s) to review on PR #<pr_number>. How do you want to handle them?`
- header: `Humans`
- multiSelect: false
- options:
  - `Paged-bulk (5/page) — recommended` — drive collection
    through `paged-bulk-mode.md`: 5 comments on screen per
    page, the user submits all 5 decisions in one prompt.
    Auto-classify is OFF for humans (see §3a); the list +
    single prompt is the entire speedup. See §3a.
  - `Walk step-by-step` — `Comment by comment, choose Fix / Reply on GitHub / Dismiss / Skip. Recorded only — apply phase runs after the walk.`
  - `Skip queue — handle later` — `Don't touch human comments in this run. The final watch verdict will flag them as pending so you remember.`

If the user ever picks `Skip queue`, emit
`HUMANS: partial pending=<N>` in §8 and return — don't process
any item.

### 3a. Paged-bulk (5 comments / page, no auto-classify)

When the user picks `Paged-bulk`, delegate **collection** to
`paged-bulk-mode.md` — the fragment is collect-only and never
runs apply routines. Reply drafting + per-reply confirmation
happens inside the fragment (when the user enters `R` for an
item, paged-bulk drafts the reply, asks the user to confirm or
edit the wording, and stores the confirmed body on the
decision entry); the actual `gh api` POST is deferred to §6
just like Fix / Dismiss.

```text
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/paged-bulk-mode.md
```

Pass the queue-specific inputs:

- `items` — the classified humans list from §1, each carrying
  `index`, `file:line` (or `"PR top-level"` for top-level
  issue comments), `link` — **REQUIRED**, the
  `/changes#r<comment.id>` URL for line-level review comments
  (NOT the comment's `html_url`, which resolves to the
  Conversation view) and the `html_url` field verbatim for
  top-level issue comments (`/pull/<n>#issuecomment-<id>` is
  the right surface for those) — `excerpt` (first ~120 chars
  of body), and the full payload the apply phases need
  (author, body, timestamp, thread id for line-level review
  comments).
- `queue_label = Humans`.
- `allowed_letters = F,R,D,S`. Meanings — `F` = Fix (Claude
  patches based on the comment); `R` = Reply (draft + confirm
  inside paged-bulk, post in §6); `D` = Dismiss (resolve
  thread, line-level only); `S` = Skip.
- `auto_classify = false`. Humans don't get pre-classification
  — their review judgement shouldn't be pre-graded by Claude.
  `paged-bulk-mode.md` omits the picks-action option and
  renders the list without a `suggest:` line.
- `picks_action_label` — not required when
  `auto_classify=false`; the picks-action option is omitted
  for humans.
- `decisions` — the output list the fragment populates.

**Abort signal still applies inside paged-bulk.** Before
chunking into pages, scan every item's body for strings like
"wait", "stop", "hold off", "I'm reviewing manually", "WIP" —
same check as §4's Walk mode. If any match, surface it
explicitly and ask the user `Abort the whole watch?` BEFORE
starting the first page. The paged-bulk pages exist to speed up
triage; they don't override the reviewer's implicit veto.

When the fragment returns, `decisions[]` carries the full queue
(with confirmed reply bodies on `R` entries). Fall through to
§5.

### 4. Walk step-by-step — per-item collection wizard

For each unresolved human comment, collect a decision; **no
apply happens here** — the apply phases (§5–§8) run after the
walk finishes.

Compose a preamble block in chat. The **Link is REQUIRED** and
depends on the comment's source:

- **Line-level review comment** (from
  `gh api pulls/:n/comments`) — construct the Files-changed URL
  explicitly; do NOT use the `html_url` field, which resolves
  to the Conversation view (`/pull/<n>#discussion_r<id>`) —
  users want the diff context:
  ```
  https://github.com/<OWNER_REPO>/pull/<PR>/changes#r<comment.id>
  ```
  `<comment.id>` is the numeric `id` field from the API
  response (not a node-id).
- **Top-level issue comment** (from `gh pr view --json comments`
  / `gh api issues/:n/comments`) — use the `html_url` field
  verbatim; resolves to `.../pull/<n>#issuecomment-<id>` which
  IS the right surface for these.

```
Human comment <i>/<N>:
  Author:   <login>
  On:       <file>:<line>  (or "PR top-level" for issue comments)
  Link:     <files#r<id> for line-level; html_url for top-level>
  Created:  <timestamp>
  Body:
    <first ~400 chars of body, wrapped>
```

Then `AskUserQuestion`:

- question: `How to handle this comment?`
- header: `Comment <i>/<N>`
- multiSelect: false
- options:
  - `Fix (Claude patches)` — `Record a Fix decision. The apply phase reads the referenced code + the comment and applies a focused change, then stages it.`
  - `Reply on GitHub` — `Draft a reply now (you'll review the wording). The actual GitHub post is sent in the apply phase.`
  - `Dismiss (resolve thread)` — `Record a Dismiss. The apply phase resolves the review thread on GitHub without changing code. Line-level comments only — top-level issue comments can't be "resolved".`
  - `Skip — decide later` — `Record a Skip; the comment stays unresolved.`

Per-option recording:

- `Fix (Claude patches)` → append `{ item, letter: F }` to
  `decisions[]`.
- `Reply on GitHub` → draft the reply body inline:
  - Print the comment body excerpt + a `Proposed reply:` block
    in chat.
  - `AskUserQuestion`:
    - question: `Reply to @<author> — confirm or edit?`
    - header: `Reply <i>/<N>`
    - options: `Post as drafted` / `Skip — don't reply`
    - The Other input lets the user paste an edited body.
  - If the user picked `Post as drafted` → use the drafted body.
    If they typed Other → use the Other text. If they picked
    `Skip — don't reply` → downgrade this item to `S` (record
    `{ item, letter: S }`) and skip the append below.
  - Append `{ item, letter: R, reply_body: <final-body> }`. The
    `gh api` POST itself is deferred to §6.
- `Dismiss (resolve thread)` → append `{ item, letter: D }`.
  For top-level issue comments (which can't be "resolved"), the
  validation in §5 catches it and downgrades it to `S` — the
  walk wizard accepts it here so the user gets the same option
  set on every item.
- `Skip — decide later` → append `{ item, letter: S }`.

**Abort signal.** If a human comment's body contains strings like
"wait", "stop", "hold off", "I'm reviewing manually", "WIP" —
treat that as an implicit signal. Surface it, ask the user
explicitly: `This looks like an abort signal. Abort the whole
watch?` with `Abort entire watch-pipelines loop` / `No, keep
going — I'll handle the comment`. On abort, emit
`HUMANS: aborted reason=<excerpt from comment>` and bubble up to
`watch-pipelines.md` §5 immediately — no apply phase runs.

After the last item, fall through to §5.

### 5. Phase B — Apply local edits and commit (no push)

`decisions[]` holds the full queue (from §3a paged-bulk or §4
walk). Walk it in order; per item:

- **`F` — Fix (Claude patches).** Read the referenced code + the
  comment, apply a focused edit (don't drift into surrounding
  code), then `git add -- "<path>"`. For line-level review
  comments, append the thread id (looked up from the GraphQL
  threads payload fetched in §1) to `FIXED_THREAD_IDS` for §6's
  resolve. Top-level issue comments have no thread to resolve
  — just stage the code change.
- **`R` — Reply.** No local action. The confirmed reply body is
  on the decision entry; append `{ item, body: reply_body }`
  to `REPLIES_TO_POST` for §6.
- **`D` — Dismiss.** Validate: line-level review comments only.
  If the item is a top-level issue comment, downgrade silently
  to Skip (append the item to `pending_items`) and continue —
  the walk wizard / paged-bulk fragment let `D` through on
  every item to keep the UI uniform; the queue-level validator
  here is what enforces the "line-level only" rule. For valid
  items, append the thread id to `DISMISS_THREAD_IDS` for §6.
- **`S` — Skip.** Append the item's `<file:line>` (or
  `top-level-<idx>`, or `@<login>` for an unanchored review) to
  `pending_items` for §8.

**Apply-time failure mode.** If a routine throws (file vanished,
edit failed to apply cleanly, etc.): fail fast. Print one line
in chat naming items already applied + the failing item. Do
**not** commit, do **not** run §6, do **not** push. `rm -rf
"$SCRATCH"` and emit at §8:

```
HUMANS: aborted reason=apply-failed-on=<file:line>
```

If at least one staged change exists after the walk, drive
`commit-from-fix.md` with `push=no`,
`fix_kind=review-comments`, and
`fix_summary="responded to <K> human review comment(s)"`.
Expect:

- `COMMIT: ok subject="…" pushed=no` → continue to §6.
- `COMMIT: skip` → no Fix landed (only Reply / Dismiss / Skip);
  skip the Fix-thread-resolve loop in §6 but still post replies
  and run Dismiss resolves.
- `COMMIT: failed` → `rm -rf "$SCRATCH"`, emit
  `HUMANS: aborted reason=commit-failed`.

### 6. Phase C — Apply remote side effects

Run remote actions in this order:

1. **Resolve threads.** Loop over
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

   Failures (403, already resolved by the reviewer, no write
   access) log + continue — the user's intent landed; the
   reviewer can always re-open the thread if they disagree.

2. **Post replies.** For each entry in `REPLIES_TO_POST`:
   - Top-level issue comment →
     `gh api repos/$OWNER_REPO/issues/$PR/comments -f body=…`.
   - Line-level review comment →
     `gh pr review $PR --comment --body "<body>"` (or
     `gh api repos/$OWNER_REPO/pulls/$PR/comments/<id>/replies`
     for an in-thread reply when the API supports it).

   Failures (network, 403) log + continue. Reply does NOT
   resolve the thread — leaves the conversation open by
   design.

Track the resolved-thread count for §8.

### 7. Phase D — Push

If §5 produced a commit (`COMMIT: ok pushed=no`), run a single
push now:

```bash
git push
```

On failure (non-fast-forward, auth, hook), do NOT retry, do
NOT force-push. `rm -rf "$SCRATCH"` and emit
`HUMANS: aborted reason=push-failed`.

On success, re-enter `watch-pipelines.md §1` — the push may
kick new CI runs and fresh review-bot passes.

If §5 emitted `COMMIT: skip` (no Fix landed), there's nothing
to push. Skip directly to §8 without re-polling — Dismiss /
Reply already landed in §6, Skip opts out.

### 8. Emit the final line

Alone on its own line, no markdown:

```
HUMANS: all-clear
HUMANS: handled committed=<count> resolved=<count>
HUMANS: partial pending=<comma-separated-short-locations>
HUMANS: aborted reason=<short-reason>
```

`committed=<count>` is the number of items that ended up in
the queue's local commit (Fix only — Reply doesn't stage
code). `resolved=<count>` is the number of review threads §6
successfully resolved (Fix on line-level + Dismiss on line-
level). Top-level issue comments and Reply don't count toward
`resolved`. May be lower than `committed` when some resolves
failed (403, already resolved, no write access) or when some
Fix items were on top-level issue comments.

`reason=` on the `aborted` line is one of:

- `<excerpt from comment>` — abort signal in a reviewer
  comment ("please hold off", "I'm reviewing manually", etc).
- `apply-failed-on=<file:line>` — Phase B (§5) hit a routine
  error on that item; nothing was committed.
- `commit-failed` — `commit-from-fix.md` returned `COMMIT: failed`.
- `push-failed` — Phase D (§7) `git push` rejected the push.

Example pending format: `AuditPanel.tsx:42,top-level-3,jlevdev`.

## Guardrails

- Never auto-fix a human comment without their decision — no
  equivalent of "Fix all" here. Humans get read and decided per
  item.
- Never post a reply on GitHub without showing the draft and
  getting explicit confirmation. Drafting + confirmation happen
  during collection (§4 walk and §3a paged-bulk both prompt for
  Reply wording inline); the actual `gh api` POST happens in
  Phase C (§6). The user always reviews wording before anything
  lands publicly.
- Only resolve a human thread when the user explicitly acted
  on it — Fix (code change landed) or Dismiss (explicit
  dismissal). Reply does NOT resolve — it leaves the
  conversation open for the reviewer to respond. Skip does
  NOT resolve — a skipped thread stays in the pending list.
  If the reviewer disagrees with a Fix, they can re-open the
  thread in GitHub's UI, same as with a bot queue.
- **Never push between phases.** Phase B uses `commit-from-fix.md`
  with `push=no`; Phase D is the single place a `git push`
  happens. Pushing inside Phase B would land code on the PR
  before §6 posts replies and resolves threads — exactly the
  back-and-forth the queue restructure exists to prevent.
- Stop immediately on any body that reads as an abort signal
  until the user confirms otherwise.
- `rm -rf "$SCRATCH"` before EVERY exit — the final line (§8), the
  empty-comments announce-and-exit (§2), and every early
  `emit … and return` abort (`apply-failed-on` / `commit-failed` in
  §5, `push-failed` in §7). None of them may leave the scratch dir
  behind.
