# watch-pipelines — strategy-driven CI watch loop

Source of truth for the CI watch loop of the standalone
`/wise-pr-watch` skill (the `ticket-auto` workflow runs an autonomous
analogue, `watch-pipelines-auto.md`).

**Must run in the main conversation, not in a Task subagent.**
The per-item wizards in §3b (sonar) and §3e (bot reviews) use
`AskUserQuestion`, which only works main-thread. The standalone
`/wise-pr-watch` skill is main-thread by construction, so this
holds automatically.

You (Claude) drive this as a long-running conversational step: poll
GitHub for the PR's check runs + new comments, react per check
class, commit autofixes via the shared `commit-from-fix.md`
fragment, and loop until either (a) every check is green and
there are no unresolved comments, or (b) you hit the abort path
from the user.

## Context the caller supplies

- `pr_number` — the PR to watch.
- `pr_url` — the PR url (for the final "all green" summary line).
- `current_branch` — the PR's head branch (so you know what to
  push to after a fix).
- `project.path` — absolute path to the repo working tree.

All sibling fragments this procedure delegates to —
`commit-from-fix.md`, `handle-human-comments.md`,
`handle-bot-reviews.md`, `handle-sonar-issues.md` — live alongside
this file in `${CLAUDE_PLUGIN_ROOT}/references/pr/`; read them from
there.

## Procedure

Run all `gh` / `git` commands with `cd <project.path>` first.

### 1. Start a poll loop

Use `gh pr checks --watch --interval 5 <pr_number>` — it blocks
until every check reaches a terminal state and prints a table of
results. The blocking call keeps you out of a tight polling loop
and avoids burning the prompt cache.

When it returns:

```bash
gh pr checks <pr_number> --json 'name,state,conclusion,link,detailsUrl,completedAt' > /tmp/wise-pr-checks-$<pr_number>.json
```

Checks are one source of unfinished business; **PR comments are
the other**. GitHub splits them across three API surfaces — the
watch loop reads all three (see
`handle-bot-reviews.md` §1 for the exact queries):

1. `gh pr view --json comments` — top-level issue comments (bot
   summaries live here).
2. `gh api repos/:/:/pulls/:n/comments` — line-level review
   comments (Copilot and CodeRabbit drop actionable suggestions
   here).
3. `gh api repos/:/:/pulls/:n/reviews` — reviews with states
   (`CHANGES_REQUESTED` on any review means work to do even if
   the top-level state is green).

For this §1, fetch the quick-look form — new human comments since
the last iteration — and surface them immediately; actionable bot
items are handled in §3e after the per-check dispatches:

```bash
LAST_SEEN="$(cat /tmp/wise-pr-lastcomment-$<pr_number> 2>/dev/null || echo 1970-01-01T00:00:00Z)"
gh pr view <pr_number> --json comments --jq \
  '.comments | map(select(.createdAt > "'"$LAST_SEEN"'")) | .[] | "[\(.createdAt)] @\(.author.login): \(.body)"'
# Then update LAST_SEEN:
date -u +%Y-%m-%dT%H:%M:%SZ > /tmp/wise-pr-lastcomment-$<pr_number>
```

If a **human** left a new comment, surface it to the user in-chat
before acting on anything else. A reviewer asking a question
usually means "stop autofixing and wait for me" — treat it as
implicit abort (see §5) unless the user confirms otherwise.

Bot top-level comments are usually summary noise (CodeRabbit skip
notice, SonarCloud gate) — don't escalate here; §3e classifies
and dispatches them properly.

### 2. Classify each failed check

Walk the JSON from §1. Ignore passed / skipped / neutral checks.
For each `FAILURE` or `CANCELLED` check, pick a class from
`name`:

| Pattern matches `name` (case-insensitive) | Class |
|---|---|
| `lint`, `eslint`, `oxlint`, `stylelint`, `prettier`, `rubocop`, `phpcs` | `lint` |
| `test`, `ci/test`, `unit`, `integration`, `e2e`, `vitest`, `jest`, `codecept`, `pytest` | `tests` |
| anything else | `other` |

Note — **`sonar` is NOT a check-failure class here**. Even when
the sonar quality-gate check passes, OPEN issues can exist on
the PR. Sonar is handled as its own reviewable queue in §4 via
`handle-sonar-issues.md`, which always runs regardless of the
check's PASS/FAIL state.

Record the mapping. If a check is ambiguous (e.g. a single check
named `ci`), prefer `other` — the `other` path asks the user
rather than guessing wrong.

### 3. Dispatch per check-failure class

Handle failing checks ONE AT A TIME, in the order they appear in
the JSON (stable wrt each poll). After each fix + commit,
re-poll (§1) so you pick up fresh results and any net-new
failures the fix exposed.

#### 3a. `lint`

- Announce: `Lint check <name> failed — pulling the log.`
- Pull the failing log:

  ```bash
  gh run view --log-failed <run-id-from-check-detailsUrl> 2>&1 | head -200
  ```

- Run the project's lint-fix:
  - JS/TS project → `cd <project.path> && npm run lint:fix` (infer
    the exact script from `package.json`).
  - Other projects → infer the fixer from `package.json` scripts,
    `composer.json`, or `Makefile`. If no fixer is obvious, fall
    through to the manual-edit path and apply the specific fix
    the log names.
- Verify the lint passes locally by re-running the same command
  without `:fix` (or the check's actual command if you can extract
  it).
- Commit using the `commit-from-fix.md` fragment with
  `fix_kind=lint`. Read the fragment
  (`${CLAUDE_PLUGIN_ROOT}/references/pr/commit-from-fix.md`) via the
  Read tool.
- If the fragment returns `COMMIT: skip`, announce "lint fixer
  ran but produced no changes — the failure may be outside the
  autofixer's scope" and escalate to `other` treatment.
- Re-enter the loop (§1).

Pass path: if `lint` passed on this iteration, announce
`Lint check <name> passed ✓` in chat and move on.

#### 3c. `tests`

- Announce: `Test check <name> failed — pulling failing tests.`
- Pull the log and extract failing test names:

  ```bash
  gh run view --log-failed <run-id> 2>&1 | grep -iE 'FAIL|✗|Error:|AssertionError' | head -30
  ```

- Attempt a fix:
  - Read the failing test file and the code under test.
  - Patch whichever side actually has the bug. When in doubt, the
    PRODUCTION code is wrong more often than the test; but if the
    test encodes stale expectations (e.g. checks for a removed
    feature), update the test.
  - Run the tests locally (`npm run test`, `vendor/bin/codecept
    run`, or the relevant project-specific command) to verify.
- If one round of fix doesn't pass locally, STOP and escalate via
  chat: describe what you tried, surface the failure, and
  `AskUserQuestion`:
  - `Fix — I'll drive the next fix round`
  - `Skip — mark the test failure as acceptable and move on`
  - `Abort watch — exit without further action`
- Commit via `commit-from-fix.md` with `fix_kind=tests`.
- Re-enter the loop.

Pass path: announce and move on.

#### 3d. `other`

For anything that doesn't match lint/sonar/tests:

- Pull the check's log (`gh run view --log-failed …`).
- Summarise the failure in chat in ≤3 sentences.
- `AskUserQuestion`:
  - `Fix — Claude drives` — Claude proposes a fix, user reviews, commit via `commit-from-fix.md` with `fix_kind=other`.
  - `Skip — accept this failure` — mark the check as "accepted" in the summary and don't act. The PR will remain with this check red.
  - `Abort watch — exit the watch loop` — §5.

### 4. Reviewable queues — four mini-pipelines

Runs **once all failed-check branches in §3 resolve** (passed,
skipped, or fixed-and-re-polled). This section is what the
watch loop spends most of its attention on in practice —
humans, Copilot, CodeRabbit, and SonarCloud each get their
own queue, their own top-level gate, and their own verdict
line.

The top-level gate for each queue is:

- **Bot queues (Copilot, CodeRabbit) and Sonar** —
  `Paged-bulk (5/page, auto-classified) — recommended` /
  `Fix all in one shot` / `Walk step-by-step` / `Skip queue`.
  Paged-bulk is the default: 5 items on screen per page with
  Claude's pre-classified decision for each, the user
  confirms or edits the whole page in one prompt. The first
  option's verb is queue-specific — bot queues read `Fix:
  <decisions>`, Sonar reads `Accept: <decisions>` (suppressing
  a Sonar rule violation is a distinct semantic from fixing a
  comment). Walk step-by-step keeps the per-item cadence for
  users who want to inspect every decision. See
  `paged-bulk-mode.md` for the shared algorithm.
- **Humans** — `Paged-bulk (5/page) — recommended` /
  `Walk step-by-step` / `Skip queue`. Auto-classify is OFF
  for humans (review judgement shouldn't be pre-graded by
  Claude); the list + single prompt is the entire speedup.
  No `Fix all in one shot` here at the top-level gate, and
  no auto-classify for humans — review judgement stays with
  the user even when using paged-bulk.

Every queue follows the same 4-phase shape after collection
finishes: **A** Collect (Walk + Paged-bulk + Fix-all all
record decisions only) → **B** Apply local edits + ONE
commit (no push, via `commit-from-fix.md` `push=no`) →
**C** Apply remote side effects (resolve threads, post
replies, run any Sonar MCP `change_issue_status` calls) →
**D** ONE `git push` at the end of the queue. This is the
"collect every decision, then commit locally, then handle
remote, then push once" cadence — designed to remove the
back-and-forth that per-item inline applies produced
pre-2.6.0.

**Why four separate queues** and not one merged list: each
source has different semantics (humans need conversation;
CodeRabbit suggestions are usually mechanical; Copilot sits
somewhere in between; Sonar issues are rule violations not
conversations), and lumping them together forced the user into
a one-size-fits-all wizard that couldn't honour the real
differences. Separate queues let the user bulk-apply
CodeRabbit, walk Copilot case-by-case, and skip Sonar for
later — all in one run, without cross-talk.

Process order: **humans → Copilot → CodeRabbit → Sonar.** Human
comments first because they're the most valuable signal and
can implicitly abort the whole loop (a human asking "please
hold off" short-circuits everything else).

#### 4.0 Resolve stale (outdated) review threads — preamble

Before any queue runs, sweep the PR for review threads GitHub
flagged as outdated (`isOutdated: true`) but that nobody marked
resolved (`isResolved: false`). The handler-level classifiers
in `handle-bot-reviews.md` §2 and `handle-human-comments.md` §1
already filter outdated items out of the actionable lists —
they're stale by construction (the lines they anchor to moved
or were deleted, so the comment no longer applies to the
current diff) — but pre-2.6.2 the workflow left them
*unresolved* on GitHub, which produced the failure mode "PR
ships with green CI but a pile of `Outdated` badges on the
Conversation tab nobody cleaned up". Resolve them as a
batch up front, log the count, then fall through to §4a.

```bash
PR=<pr_number>
OWNER_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"

# Pull every thread on the PR with its outdated + resolved flags
# and its node id (the GraphQL `id` field — needed by
# resolveReviewThread, NOT the comment databaseId).
gh api graphql -f query='
  query($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        reviewThreads(first: 100) {
          nodes { id isResolved isOutdated }
        }
      }
    }
  }
' -f owner="${OWNER_REPO%/*}" -f repo="${OWNER_REPO#*/}" -F number=$PR \
  > /tmp/pr-$PR-threads-preamble.json

STALE=0
RESOLVED=0
for THREAD_ID in $(jq -r '
  .data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isOutdated == true and .isResolved == false)
  | .id
' /tmp/pr-$PR-threads-preamble.json); do
  STALE=$((STALE + 1))
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

If `STALE` is 0, print nothing and continue to §4a — silence
on a clean PR. Otherwise print one chat line:

```text
Resolved <RESOLVED> outdated review thread(s) on PR #<pr_number>.
```

When `RESOLVED < STALE` (some calls failed — 403, no write
access, thread already resolved by someone else between fetch
and mutation), append `(<STALE - RESOLVED> failed; left for
manual cleanup)` to the same line. Failures log + continue —
the actionable queues still run regardless.

This sweep runs **once per §4 entry**, which means it re-runs
on every iteration of the watch loop (re-poll after a queue's
Phase D push lands back at §1, then §4, then §4.0 again). A
push can create new outdated threads (e.g. CodeRabbit re-runs
on the new commit and re-flags older lines), so re-running is
correct.

For each queue, delegate to its fragment:

#### 4a. Human comments

```
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/handle-human-comments.md
```

Context: `pr_number`, `pr_url`, `project.path`.
Emits `HUMANS: <all-clear | handled committed=N resolved=M | partial pending=… | aborted reason=…>`.
The `resolved=M` key on the `handled` line counts line-level
threads auto-resolved after a Fix landed; see
`handle-human-comments.md` §5–§6 for the full semantics.

On `aborted`, bubble up to §6 immediately. On any other verdict,
move to §4b.

#### 4b. Copilot queue

```
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/handle-bot-reviews.md
```

Context: the usual + `bot_filter=copilot` + `bot_display_name=Copilot`.
Emits `BOT-REVIEWS: … bot=copilot` — on `handled`, includes
`committed=N resolved=M` (Fix / Fix-using-suggestion / Dismiss
all auto-resolve the thread on GitHub in Phase C; see
`handle-bot-reviews.md` §6).

#### 4c. CodeRabbit queue

```
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/handle-bot-reviews.md
```

Context: the usual + `bot_filter=coderabbit` +
`bot_display_name=CodeRabbit`. Emits `BOT-REVIEWS: … bot=coderabbit`.

#### 4d. SonarCloud issues

```
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/handle-sonar-issues.md
```

Context: `pr_number`, `pr_url`, `current_branch`, `project.path`.
Emits `SONAR: <all-clear | handled committed=N |
partial pending=N | unchecked reason=… | aborted reason=…>`.

#### 4e. Re-poll after any committed batch

If any of §4a–§4d emitted `handled committed=<N>` (meaning code
was pushed), re-enter §1 before declaring green — the push may
have kicked a new CI run and a new pass from the bots. Run §4
again only for the queues that had items in the LAST iteration
(no need to re-query queues that were `all-clear`).

If none of the queues committed anything (all `all-clear` /
`partial` / `unchecked`), proceed to §5.

### 5. All green + all queues resolved

When a poll comes back with every non-skipped check as
`conclusion: SUCCESS` AND every §4 queue returned `all-clear`
OR `partial` OR `unchecked` (no `aborted`):

- Announce `All checks passed ✓` in chat.
- If any queue was `partial` / `unchecked`, list the markers
  the user should know about: `with the following left for
  you to handle: humans-skipped, sonar-unchecked, …`.
- Print the PR url:
  `PR is ready for review: {{pr_url}}`
- Emit the final line (§7).

### 6. Abort paths

Any of these conditions exits the loop:

- User picked `Abort watch` anywhere in §3 or §4.
- Any queue fragment emitted `aborted reason=…`.
- A `git push` inside `commit-from-fix.md` returned `COMMIT: failed`.
- More than 10 loop iterations without progress (a safety catch —
  if we're looping without reducing the failure count, something
  is stuck; escalate via chat and stop).

### 7. Emit the final line

Your response's FINAL line — alone on its own line, no markdown,
no backticks — MUST match one of:

```
WATCH: all-green url=<url>
WATCH: aborted reason=<short-reason>
WATCH: partial url=<url> accepted=<comma-separated-check-names>
```

`partial` means at least one of:

- A check was marked "accepted" by the user during §3 — the
  PR is ready for review but not every check is green.
- A §4 queue returned `partial pending=<…>` (user picked `Skip
  queue` at the top-level gate, OR skipped individual items in
  the interactive walk). Use per-queue markers in the rolled-up
  list:
  - `humans-skipped=<N>` / item locations when walking
  - `copilot-skipped=<N>` / locations
  - `coderabbit-skipped=<N>` / locations
  - `sonar-skipped=<N>`
- A §4 queue returned `unchecked` (Sonar fetch failure, user
  picked `Mark unchecked`). Marker: `sonar-unchecked`.

When emitting `partial`, `accepted` should include ALL
applicable markers. Examples:

```
WATCH: partial url=<url> accepted=sonar-unchecked
WATCH: partial url=<url> accepted=copilot-skipped=3,AuditPanel.tsx:42
WATCH: partial url=<url> accepted=humans-skipped=1,coderabbit-skipped=5,sonar-unchecked
```

The workflow's `report` step surfaces this verbatim so the user
sees in one line what's left.

## Guardrails

- Never force-push.
- Never `--no-verify` a commit.
- Never run a fix on a file the user owns visibly (files in their
  worktree that aren't part of the failing check's scope).
- Never accept a Sonar issue without a rationale comment.
- Stop on abnormal conditions — stuck loops, push failures,
  unexpected network errors — rather than retrying blindly.
- If a comment explicitly asks you to stop ("please hold off",
  "I'm reviewing manually", etc), treat it as an implicit
  `Abort watch` and jump to §6 even if the user hadn't confirmed
  it via AskUserQuestion.
