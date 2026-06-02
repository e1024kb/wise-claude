# watch-pipelines-auto — autonomous CI watch + fix loop

Autonomous analogue of `references/pr/watch-pipelines.md`.
Polls the PR's CI, auto-fixes failing checks, waits for CodeRabbit /
Copilot to finish reviewing, classifies every bot review comment by
severity, fixes or dismisses each one, commits + pushes, and loops
until the PR is fully resolved or a cap is hit. It only merges once
CI is green AND both bots have reviewed AND every bot comment is
fixed-or-dismissed. It NEVER calls `AskUserQuestion` — every decision
the interactive watcher escalates to the user is made autonomously by
the **Lead Architect** persona and recorded.

Source of truth for the `/wise-pr-watch-auto` skill and the
`ticket-auto` workflow's watch step.

## Context the caller supplies

- `pr_number`, `pr_url` — the PR to watch.
- `current_branch` — the PR's head branch.
- `project.path` — absolute path to the repo working tree (a ticket
  worktree, when called from `ticket-auto`).
- `max_fix_attempts` — cap on commit-producing fix rounds (default 10).
- `ticket_ref`, `plan_path` — **optional** ticket context. Passed
  straight through to `handle-bot-reviews-auto.md` so the
  major/critical path can weigh a bot concern against the ticket.
- `config_prompt` — **optional** operator standing guidance (may be
  empty). Honor its guardrails when deciding what to auto-fix (e.g.
  files to stay out of), and pass it through to
  `handle-bot-reviews-auto.md` so the bot-comment path weighs it too.

## Procedure

Run all `gh` / `git` commands with `cd <project.path>` first. Keep a
counter `ATTEMPTS = 0` and an iteration counter `ITERS = 0`.

### 1. Poll the checks

```bash
gh pr checks <pr_number> --watch --interval 10
gh pr checks <pr_number> --json name,state,conclusion,link,detailsUrl > /tmp/ticket-auto-checks-$<pr_number>.json
```

`--watch` blocks until every check reaches a terminal state. Then
check for a **human** comment since the run started:

```bash
gh pr view <pr_number> --json comments \
  --jq '.comments[] | select((.author.login | test("(?i)copilot|coderabbit|sonar|bot$") | not)) | .author.login'
```

If a non-bot human has commented, **stop immediately** — never fight
a reviewer. Emit `WATCH-AUTO: human-intervention url=<pr_url>`.

### 2. Classify failing checks

For each check with `conclusion` `FAILURE` / `CANCELLED`, classify by
`name` (case-insensitive): `lint|eslint|oxlint|prettier|rubocop|phpcs`
→ `lint`; `test|unit|integration|e2e|vitest|jest|pytest|codecept` →
`tests`; anything else → `other`.

### 3. Fix failing checks (autonomous)

Handle failures one at a time. After each fix that produces a commit,
increment `ATTEMPTS`; if `ATTEMPTS >= max_fix_attempts`, stop and emit
`WATCH-AUTO: exhausted url=<pr_url>` with the last failing check's
name. Honor `config_prompt` guardrails throughout: a fix must not edit
a file the operator told the run to avoid (or otherwise cross a stated
guardrail) — if the only available fix would, leave the check
`accepted` and record it rather than crossing the guardrail. For each
failure:

- Pull the failing log: `gh run view --log-failed <run-id> 2>&1 | head -200`.
- **lint** — run the project's lint-fix (`npm run lint:fix`, or infer
  from `package.json` / `composer.json` / `Makefile`); verify locally.
- **tests** — read the failing test + the code under test, patch
  whichever side has the real bug, verify locally. Allow **up to 2**
  fix rounds for one test check; still failing → leave it, mark
  `accepted`, continue (do not abort the whole run for one check).
- **other** — attempt one fix from the log; if it does not pass
  locally, mark the check `accepted` and continue.
- Commit each fix via `${CLAUDE_PLUGIN_ROOT}/references/pr/commit-from-fix.md`
  with the matching `fix_kind` and `push=yes`.
- After a committed fix, re-enter §1 (re-poll).

### 4. Wait for the review bots to finish

CI checks settling does NOT mean the review bots are done — CodeRabbit
and Copilot post review comments asynchronously, and they are not CI
checks. Once every check is green or `accepted`, wait for the bots
before evaluating comments or merging.

```bash
HEAD_SHA="$(git rev-parse HEAD)"
OWNER_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
BOT_REVIEW_POLL=20          # seconds between polls
BOT_REVIEW_TIMEOUT=900      # 15 min wall-clock cap

bot_review_done() {   # $1 = login regex
  gh api "repos/$OWNER_REPO/pulls/<pr_number>/reviews?per_page=100" --paginate \
    --jq "any(.[]; (.user.login|test(\"$1\";\"i\")) and .commit_id==\"$HEAD_SHA\")"
}
bot_present() {       # $1 = login regex — has the bot EVER touched this PR?
  gh api "repos/$OWNER_REPO/pulls/<pr_number>/reviews?per_page=100" --paginate \
    --jq "any(.[]; .user.login|test(\"$1\";\"i\"))"
}
```

- **Determine which bots are present.** A bot that is not installed on
  the repo never produces a review — waiting on it would always time
  out. With Copilot regex `copilot` and CodeRabbit regex `coderabbit`,
  call `bot_present` for each (checking both `reviews` and
  `gh pr view --json comments` for a footprint). Only wait on bots
  with a footprint. A repo with neither bot skips this step entirely.
- **Poll.** Every `BOT_REVIEW_POLL` seconds, call `bot_review_done`
  for each present bot against the current `HEAD_SHA`. Exit the wait
  the moment every present bot returns `true`.
- **Timeout.** If `BOT_REVIEW_TIMEOUT` elapses with a present bot
  still not done, do NOT merge — emit
  `WATCH-AUTO: human-intervention url=<pr_url> reason=bot-review-timeout`
  and stop.

`BOT_REVIEW_POLL` / `BOT_REVIEW_TIMEOUT` are tunable constants.

### 5. Address bot review comments (autonomous — severity-aware)

For `bot_filter` in `copilot`, then `coderabbit` (only bots found
present in §4): read
`${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/handle-bot-reviews-auto.md`
and follow it end to end with `pr_number`, `pr_url`, `current_branch`,
`project.path`, `bot_filter`, `bot_display_name`
(`Copilot` / `CodeRabbit`), `head_sha=$HEAD_SHA`, and `ticket_ref` /
`plan_path` / `config_prompt` when supplied.

That fragment classifies every comment by severity, fixes minors
quickly, applies a considered "consolidated decision" to
major/critical ones, dismisses false positives with a reasoned reply,
and resolves every handled or dismissed thread. Capture each
`BOT-REVIEWS-AUTO:` verdict:

- Roll every `blocked=<file:line;...>` list (across both bots) into a
  single `BLOCKED` set.
- Collect any `aborted` reason.
- Note whether either invocation reported `committed=yes`.

If either bot reported `committed=yes`, a push happened — increment
`ATTEMPTS` and **re-enter §1**. The push re-triggers CI AND a fresh
CodeRabbit / Copilot pass, so §4 re-waits on the new `HEAD_SHA` and §5
re-handles. Keep looping §1 → §4 → §5 until a §5 pass reports
`committed=no` from every present bot (the loop is stable — no fix is
pending re-review), bounded by `ATTEMPTS >= max_fix_attempts` and the
§6 stuck-loop catch.

**Sonar issues are not auto-suppressed.** If a Sonar quality-gate
check is failing, treat it as an `other` check in §3 (attempt a real
fix); never suppress a Sonar rule autonomously — record any unfixed
Sonar issues for the report instead.

### 6. Safety cap

If `ITERS` (incremented once per §1 poll) exceeds 10 without the
failing-check count going down, stop — something is stuck. Emit
`WATCH-AUTO: exhausted url=<pr_url>` with `reason=stuck-loop`.

### 7. Merge when fully resolved

Merge the PR — and only when **all** of these hold:

1. every non-skipped CI check is `SUCCESS`,
2. both present bots completed review of the current `HEAD_SHA` (§4),
3. the last §5 pass reported `committed=no` from every present bot —
   the loop is stable, no fix is pending re-review,
4. every handled or dismissed bot comment is a resolved thread on the
   PR — every §5 bot invocation returned `handled` (or `all-clear`),
   none returned `aborted` with `reason=unresolved-threads`,
5. the rolled-up `BLOCKED` set is empty,
6. no §5 bot invocation emitted `aborted`.

```bash
gh pr merge <pr_number> --squash
```

`gh pr merge` respects the repo's settings and branch protection. If
it fails because the repo disallows squash, retry once with
`--merge`. If it fails for any other reason — branch protection
requires a human approval, a merge conflict, etc. — do NOT force it:
leave the PR green and open for a human, and record why.

If the `BLOCKED` set is non-empty (5 fails), leave the PR open and go
to §8 with the `blocked` verdict. If a §5 invocation `aborted` (6
fails), leave the PR open and emit `partial` / `exhausted` per the
abort reason. Never merge on any non-merged verdict — those PRs are
always left open.

### 8. Terminal verdict

Emit, as the FINAL line — alone, no markdown, no backticks — one of:

```
WATCH-AUTO: merged url=<pr_url>
WATCH-AUTO: all-green url=<pr_url> reason=<why-not-merged>
WATCH-AUTO: blocked url=<pr_url> items=<file:line;file:line;...>
WATCH-AUTO: partial url=<pr_url> accepted=<comma-separated-markers>
WATCH-AUTO: exhausted url=<pr_url> reason=<lint|tests|other|stuck-loop>
WATCH-AUTO: human-intervention url=<pr_url> [reason=bot-review-timeout]
```

- `merged` — every check green, both bots reviewed, every bot comment
  fixed-or-dismissed and resolved, PR merged.
- `all-green` — every check green and every bot comment resolved, but
  the merge was blocked (branch protection / required approval /
  conflict); PR left open.
- `blocked` — CI green and both bots reviewed, but at least one
  non-minor bot comment could not be confidently resolved; `items=`
  names every blocked `file:line`; PR left open for a human.
- `partial` — green except checks marked `accepted`, or a bot queue
  aborted (`accepted=tests-accepted,sonar-open=2`).
- `exhausted` — `max_fix_attempts` or the stuck-loop catch hit.
- `human-intervention` — a human commented (the loop stood down), or
  `reason=bot-review-timeout` (a present bot never finished reviewing).

Only a `merged` verdict closes the PR; every other verdict
(`all-green` / `blocked` / `partial` / `exhausted` /
`human-intervention`) leaves it open for a human.

## Guardrails

- Never force-push, never `--no-verify`.
- Never merge before both present bots have reviewed the current
  `HEAD_SHA`, and never merge past an unresolved non-minor bot comment
  (a `blocked` verdict leaves the PR open).
- Merge only a fully resolved PR — never force a merge or override
  branch protection; a blocked merge leaves the PR open, it does not
  fail the run.
- Never suppress a Sonar issue autonomously — fix or report it.
- Stand down the moment a human comments on the PR.
- Stop cleanly at the attempt cap and the stuck-loop catch — an
  autonomous run must not churn forever.
- All work runs inside this Claude Code session with native tools
  (`Bash`, `Read`, `Edit`/`Write`). Never shell out to `claude -p`,
  another agent CLI, or any external LLM tool.
