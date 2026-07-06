# handle-sonar-issues — SonarCloud open issues queue (Paged-bulk / Fix all / Walk step-by-step)

Dedicated fragment for the SonarCloud issues mini-pipeline,
called from `watch-pipelines.md` §4 after the human + bot-review
queues. Factored out of the earlier in-line §3b so it follows the
same shape as the other reviewable queues (consistent top-level
gate, per-queue verdict line). Unlike the bot/human queues, this
one has **no Skip** — every fetched issue is Fixed or Accepted so
the PR carries 0 open issues.

**Why this matters — pass doesn't mean clean.** SonarCloud's
quality gate is about "new code" thresholds; OPEN issues can
exist on the PR while the gate is green. A naïve "pass → move
on" handler misses every one of them. This fragment always
fetches the issues, regardless of the check's PASS/FAIL state.

**Critical — NEVER silently claim "zero issues" when the fetch
failed.** Distinguish four outcomes explicitly:
- `OK (N issues)` — real query returned N items.
- `OK (0 issues)` — real query returned empty; safe to move on.
- `AUTH-FAIL` — 401 / 403 / missing token; fetch didn't happen.
- `FETCH-FAIL` — network error, bad componentKey, MCP error.

Only the OK buckets proceed. The FAIL buckets route to the user
(§3b below), which can mark the queue as `unchecked` so the final
watch verdict surfaces the gap instead of hiding it.

## Context the caller supplies

- `pr_number`
- `pr_url`
- `current_branch`
- `project.path`

Sibling fragments this handler reads (`commit-from-fix.md`,
`paged-bulk-mode.md`) live alongside it in
`${CLAUDE_PLUGIN_ROOT}/references/pr/`.

## Procedure

Run all `gh` / `curl` / `git` commands with `cd <project.path>`
first.

### 1. Discover the SonarCloud component key (authoritative)

The cleanest source is SonarCloud's bot comment on the PR —
its link to the issues page contains the `id=<key>` query
param, which IS the project key. Try that first; fall back to
config files; fall back to the `<org>_<repo>` convention last.

```bash
PR=<pr_number>
OWNER_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/wise-pr-XXXXXX")"

# a) SonarCloud bot comment — parse id=<key> from the issues URL.
SONAR_KEY="$(gh api "repos/$OWNER_REPO/issues/$PR/comments" \
  --jq '.[] | select(.user.login | test("sonar"; "i")) | .body' \
  | grep -oE 'sonarcloud\.io/[^)"]*[?&]id=[^&)"[:space:]]+' \
  | head -1 | sed -E 's/.*[?&]id=([^&)"[:space:]]+).*/\1/')"

# b) sonar-project.properties.
if [ -z "$SONAR_KEY" ] && [ -f sonar-project.properties ]; then
  SONAR_KEY="$(grep -E '^sonar\.projectKey[[:space:]]*=' sonar-project.properties \
    | head -1 | awk -F= '{gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2}')"
fi

# c) pom.xml for Maven.
if [ -z "$SONAR_KEY" ] && [ -f pom.xml ]; then
  SONAR_KEY="$(grep -oE '<sonar\.projectKey>[^<]+</sonar\.projectKey>' pom.xml \
    | sed 's/<[^>]*>//g')"
fi

# d) Last resort — the common <org>_<repo> convention. Mark as a guess.
if [ -z "$SONAR_KEY" ]; then
  SONAR_KEY="$(echo "$OWNER_REPO" | tr '/' '_')"
  SONAR_KEY_GUESSED=true
fi
```

### 2. Fetch the issues — prefer the Sonar MCP

If Claude Code sees any tool matching `mcp__*sonar*__*`
(the MCP naming convention for Sonar servers — e.g.
`mcp__*__search_sonar_issues_in_projects`,
`mcp__*__issues_search`, or similar), **prefer it**. The MCP's
stored credentials handle auth transparently and sidestep the
401-on-private-project trap.

Fallback order if no MCP is visible:

1. **`$SONAR_TOKEN`-authenticated curl** (when the env var is set):
   ```bash
   curl -fsSL -u "$SONAR_TOKEN:" \
     "https://sonarcloud.io/api/issues/search?componentKeys=$SONAR_KEY&pullRequest=$PR&issueStatuses=OPEN,CONFIRMED&resolved=false&ps=500" \
     > "$SCRATCH/sonar-issues-$PR.json"
   ```
2. **Anonymous curl** (public projects only).

**The issues-search endpoint is authoritative.** Do **not** run
a separate "sanity-check" probe against the project key
(`/api/components/show`, `/api/projects/search`, etc.) and then
escalate the queue to FETCH-FAIL on its 404 while the
issues-search call itself returned 200. SonarCloud's permission
model lets a token read issues without granting read access to
the component metadata, so a key that 404s on `components/show`
can still be the correct key for this PR. The pre-2.6.3 LLM-
improvised sanity-check produced a "200/0 issues but key 404'd"
deadlock that asked the user 4 questions when the right answer
was always "trust the 200/0 and emit all-clear".

Outcome buckets (decided by the issues-search call alone, no
separate probes):
- MCP call succeeded / curl exit 0 (any count, including 0) →
  `OK`; parse results.
- Issues-search HTTP 401 / 403 OR `$SONAR_TOKEN` unset on a
  private project → `AUTH-FAIL`.
- Issues-search HTTP 404 (truly bad `SONAR_KEY`) →
  `FETCH-FAIL`.
- Network error / MCP error on the issues-search call /
  anything else → `FETCH-FAIL`.

### 3. Top-level gate

Build the SonarCloud issues URL once for the upcoming branches —
all three sub-gates print it inline so the user can verify the
result by clicking, regardless of which path triggers:

```text
SONAR_URL="https://sonarcloud.io/project/issues?id=$SONAR_KEY&pullRequest=$PR&issueStatuses=OPEN,CONFIRMED"
```

#### 3a. On OK with 0 issues

Announce `Sonar: 0 open issues ✓` in chat AND print the
verifiable URL on the next line:

```text
Sonar: 0 open issues ✓
  Sonar page: <SONAR_URL>
```

If `SONAR_KEY_GUESSED=true`, append a one-line note:
`(SONAR_KEY was guessed — click to confirm.)`. The user can
sanity-check from one click; we do not pre-emptively ask.
Emit `SONAR: all-clear`. Skip the rest.

#### 3b. On AUTH-FAIL / FETCH-FAIL

Print the URL inline FIRST so the user can triage manually
without picking it from a wizard option:

```text
SonarCloud issues couldn't be fetched (<reason: auth / fetch / bad-key>).
  Sonar page: <SONAR_URL>
  (Open this in your browser to triage manually.)
```

Then `AskUserQuestion` with a smaller, action-only option set
(no "Open issues page" option — the URL is already on screen):

- question: `How do you want to proceed?`
- header: `Sonar fetch`
- multiSelect: false
- options (3, AskUserQuestion's text-input handles edge cases):
  - `Mark unchecked — keep going` — emit
    `SONAR: unchecked reason=<auth|fetch|bad-key>`. The caller
    MUST include `sonar-unchecked` in the final watch verdict.
  - `Set SONAR_TOKEN and retry` — print setup instructions
    (`https://sonarcloud.io/account/security → Generate Token →
    export SONAR_TOKEN=<token> in your shell → re-run`), then
    mark unchecked + return (env vars don't propagate into a
    running Claude Code session).
  - `Abort watch` — emit `SONAR: aborted reason=<short-reason>`.

Return without processing any items. Do **not** offer "Trust
0-issues result" — by construction we only reach §3b when the
issues-search call itself failed, so there's no 0-issues result
to trust.

#### 3c. On OK with N > 0 issues

`AskUserQuestion`:

- question: `Sonar: <N> open issue(s) on PR #<pr_number>. How do you want to handle them?`
- header: `Sonar`
- multiSelect: false
- options:
  - `Paged-bulk (5/page, auto-classified) — recommended` —
    drive collection through `paged-bulk-mode.md`: 5 issues on
    screen per page with Claude's pre-classified decision for
    each (`F` for mechanical rules; `A` for false-positive-
    prone rules with a suppression + rationale), the user
    confirms or edits the whole page in one prompt. The
    fragment only records decisions; the handler's apply
    phases (§5–§9) edit files, commit, run any Sonar MCP
    status changes, and push. See §3e.
  - `Fix all in one shot` — synthesise a Fix decision for
    every issue and run the apply phases as if the user had
    typed `F` for every item. The §5 Fix path patches what's
    mechanical and falls back to a NOSONAR-style suppression
    when a patch would change behavior. Pick this when issues
    look mechanical (unused imports, cognitive-complexity
    refactors) and you want the queue cleared.
  - `Walk step-by-step` — per-issue wizard (§4). Each issue
    gets Fix / Accept (suppression). Wizard records decisions
    only; the apply phases run after the walk.

There is **no `Skip queue` option and no per-item Skip** — a
fetched Sonar issue is always Fixed or Accepted so the PR ships
with **0 open issues**. (Skip lived here pre-3.x and let PRs
merge with open issues; that's the gap this queue now closes.)
The only "leave it" escape is a *fetch failure* — §3b's
`unchecked` — because issues you can't enumerate can't be
resolved.

### 3d. Fix all in one shot

Synthesise a `decisions[]` list with `letter: F` for every
issue (no UI step). Then jump to §5. The §5 Fix path picks the
best available action per issue (mechanical patch when
possible; project-appropriate suppression annotation +
rationale comment when a patch would change behavior).

### 3e. Paged-bulk (5 issues / page, auto-classified)

When the user picks `Paged-bulk`, delegate **collection** to
`paged-bulk-mode.md`:

```text
Read: ${CLAUDE_PLUGIN_ROOT}/references/pr/paged-bulk-mode.md
```

Pass the queue-specific inputs:

- `items` — the classified issues from §2, each carrying:
  - `file:line` — the issue's `component:line`.
  - `link` — **REQUIRED**, the SonarCloud
    `https://sonarcloud.io/project/issues?id=<SONAR_KEY>&pullRequest=<PR>&open=<issue.key>`
    URL. The user clicks this row to land on the rule
    detail + flow graphs without leaving Claude Code; if it's
    missing the page is useless for triage.
  - `excerpt` — first ~120 chars of `message`.
  - The full issue payload the apply phases need (`key`,
    `rule`, `severity`, `type`, `component`, `line`,
    `message`).
- `queue_label = Sonar`.
- `allowed_letters = F,A`. Meanings — `F` = Fix (patch the
  file per the rule); `A` = Accept (add the project's Sonar
  suppression annotation with a one-line rationale; prefer the
  Sonar MCP's `change_issue_status` when available). `S` (Skip),
  `D` and `R` are not valid in this queue — every fetched issue
  must be Fixed or Accepted, so `paged-bulk-mode.md` rejects them
  on Custom input.
- `auto_classify = true`. Pre-classification heuristics in
  `paged-bulk-mode.md` §2 (Sonar branch) — mechanical rules
  (unused import, missing `const`, naming violations) steer
  toward `F`; false-positive-prone rules (cognitive
  complexity, nesting depth) steer toward `A` with a
  suppression rationale; ambiguous rules default toward `F`
  (the §5 Fix path falls back to a suppression when a patch
  would change behavior) — never `S`.
- `picks_action_label = Accept`. The first option of each page
  reads `Accept: <decisions-string>` (e.g.
  `Accept: 1A 2F 3F 4A 5A`). **Sonar keeps the "Accept"
  wording** — adding a NOSONAR-style suppression is genuinely
  a distinct semantic ("we are choosing to live with this
  rule violation"), not a fix; the bot review queues use
  `Fix:` because every positive letter on a comment ultimately
  produces a fix. Don't normalise this to `Fix` in a future
  edit — the asymmetry is deliberate.
- `decisions` — the output list the fragment populates.

When the fragment returns, `decisions[]` carries the full
queue. Fall through to §5.

### 4. Walk step-by-step — per-item collection wizard

Build a user-visible list:

```
1. AuditPanel.tsx:42 — cognitive complexity 18 > 15 (Refactor)
2. SectionQuery.php:101 — unused import "UnusedTrait"
…
```

Per issue, compose a preamble with a **Link to the issue on
SonarCloud** so the user can click through to read the rule
detail, flow graphs, and surrounding code context without
leaving Claude Code for long:

```
Sonar issue #<i> of <N>:
  Rule:     <rule>  (<severity> / <type>)
  File:     <component>:<line>
  Link:     https://sonarcloud.io/project/issues?id=<SONAR_KEY>&pullRequest=<PR>&open=<issue.key>
  Message:  <first ~200 chars of message>
```

The issue object from the API exposes `key`, `rule`, `severity`,
`type`, `component`, `line`, `message` — plug them into the
template verbatim. The `Link:` format (with `&open=<issue.key>`)
jumps SonarCloud's UI straight to the specific issue.

Walk via `AskUserQuestion` (max 5 per question because of the
4-option cap — run multiple rounds when >5). The wizard
records decisions; **no apply happens here**. Per issue:

- `Fix (Claude edits)` — `Record a Fix decision. The apply phase reads the file + Sonar's message and applies a focused patch.`
- `Accept (add suppression)` — `Record an Accept decision. The apply phase writes a minimum-scope Sonar suppression annotation with a rationale comment, OR (when the Sonar MCP exposes change_issue_status) marks the issue "won't fix" server-side without touching code. "Accept" is intentional here: suppressing a Sonar finding is a deliberate acceptance of the rule violation, distinct from a fix.`

Every issue must be Fixed or Accepted — there is **no Skip
option**; the queue drives to 0 open issues.

Per-option recording — append to `decisions[]`:

- `Fix (Claude edits)` → `{ item, letter: F }`.
- `Accept (add suppression)` → `{ item, letter: A }`.

After the last item, fall through to §5.

### 5. Phase B — Apply local edits and commit (no push)

`decisions[]` holds the full queue. Walk it in order; per
item:

- **`F` — Fix.** Read the referenced file + Sonar's `message`.
  Apply a focused patch addressing the rule violation. Don't
  touch code outside the rule's scope. Then
  `git add -- "<component-path>"`.
- **`A` — Accept.** Two cases:
  - **MCP available.** If Claude Code sees a Sonar MCP tool
    matching `mcp__*sonar*__change_issue_status` (or similar),
    queue a `change_issue_status` call to mark the issue
    "won't fix" / "false positive" server-side. No code
    change, no `git add`. Append the issue id to a queue-
    level `MCP_STATUS_CALLS` list for §6 to drive — the call
    itself fires in Phase C, after the local commit lands,
    so we don't change Sonar state on a queue that ultimately
    aborts.
  - **No MCP — local suppression.** Add the project's
    suppression annotation (`// NOSONAR` in JS/TS/Java,
    `@SuppressWarnings("…")` in Java, `#[allow(…)]` in Rust)
    at the issue's `line` with a one-line rationale comment.
    Suppressions without rationale are a review red flag.
    Then `git add -- "<component-path>"`.

Only `F` and `A` reach this phase — `decisions[]` never carries
an `S` for Sonar (the queue has no Skip), so every fetched issue
is resolved.

**Apply-time failure mode.** If a routine throws (file
vanished, edit failed, etc.): fail fast. Print one line
naming items already applied + the failing item. Do not
commit, do not run §6, do not push. Emit at §9:

```
SONAR: aborted reason=apply-failed-on=<file:line>
```

If at least one staged change exists after the walk, drive
`commit-from-fix.md` with `push=no`, `fix_kind=sonar`, and
`fix_summary="applied <K> SonarCloud issue(s)"`. Expect:

- `COMMIT: ok subject="…" pushed=no` → continue to §6.
- `COMMIT: skip` → no Fix / local Accept landed (only
  MCP-Accept decisions); continue to §6 (the MCP calls still need
  to fire), skip §7's push, and go to §9 — no push needed.
- `COMMIT: failed` → emit
  `SONAR: aborted reason=commit-failed`.

### 6. Phase C — Apply remote side effects

For every issue in `MCP_STATUS_CALLS`, invoke the Sonar MCP
`change_issue_status` tool now, marking the issue per the
decision (typically "WONTFIX" or "FALSE-POSITIVE", whichever
the MCP's schema accepts). Failures log + continue — the
local commit (when present) already landed, and the user can
re-trigger the MCP call in a follow-up run if needed.

If `MCP_STATUS_CALLS` is empty, this phase is a no-op.

### 7. Phase D — Push

If §5 produced a commit (`COMMIT: ok pushed=no`), run a single
push now:

```bash
git push
```

On failure, do NOT retry, do NOT force-push. Emit
`SONAR: aborted reason=push-failed`.

On success, re-enter `watch-pipelines.md §1` — the push may
trigger fresh Sonar analysis on the PR.

If §5 emitted `COMMIT: skip` (all MCP-Accept — nothing local
landed), there's nothing to push. Skip directly to §9 without
re-polling.

### 8. Fall-back annotations when the CHECK is FAILED

When the sonar CHECK is `FAILURE` (not just passing with open
issues) AND steps 2/3 couldn't produce an issue list, pull
annotations the GitHub check run may have:

```bash
CHECK_ID="<id from the check's detailsUrl>"
gh api "repos/$OWNER_REPO/check-runs/$CHECK_ID/annotations" \
  --jq '.[] | {path, start_line, annotation_level, title, message}'
```

If annotations are empty too, fall back to
`gh run view --log-failed <run-id> | head -300`.

These fall back into §4 (Walk step-by-step) with the
annotations playing the role of "issues" — Phase A still
collects, Phase B/C/D apply.

### 9. Emit the final line

Alone on its own line:

```
SONAR: all-clear                                    # 0 issues found
SONAR: handled committed=<N>                        # all applied + pushed (or MCP-only when committed=0)
SONAR: unchecked reason=<auth|fetch|bad-key>        # fetch failed, user picked unchecked
SONAR: aborted reason=<short-reason>                # apply / commit / push failure or user abort
```

There is no `partial pending` line — the queue has no Skip, so a
successful fetch always ends `all-clear` (0 issues) or `handled`
(every issue Fixed/Accepted). `unchecked` is reserved for a
fetch failure (§3b), the one case where issues can't be
enumerated.

`reason=` on the `aborted` line is one of:

- `apply-failed-on=<file:line>` — Phase B (§5) hit a routine
  error on that issue; nothing was committed.
- `commit-failed` — `commit-from-fix.md` returned `COMMIT: failed`.
- `push-failed` — Phase D (§7) `git push` rejected the push.
- `<short-reason>` — user picked Abort during §3b.

## Guardrails

- **Zero open issues on a successful fetch.** Every fetched issue
  ends Fixed or Accepted — there is no Skip and no `partial
  pending`. The only non-resolved outcome is `unchecked`, reserved
  for a fetch failure (§3b) where issues can't be enumerated.
- Never silently write `[]` on a 401 — see §3b. AUTH-FAIL must
  reach the user.
- Never apply a "Fix all" suppression without a rationale
  comment.
- Never claim `all-clear` unless an actual successful query
  returned zero items. If you're unsure, emit `unchecked`.
- Never guess the `componentKey` without marking
  `SONAR_KEY_GUESSED=true` so a later 404 becomes `FETCH-FAIL`
  rather than mystery silence.
- **The issues-search endpoint is authoritative; do not run
  separate sanity-check probes against the project key.**
  SonarCloud's permissions allow a token to read issues
  without granting read access to component metadata, so
  probing `/api/components/show` (or similar) and treating
  its 404 as FETCH-FAIL produces a "200/0 issues but key
  404'd" deadlock that asked the user 4 questions when the
  right answer was always "trust the 200/0 and emit
  all-clear". Decide outcome buckets from the issues-search
  call alone.
- **Always print the SonarCloud URL inline.** Both `all-clear`
  (§3a) and the FETCH-FAIL wizard (§3b) print it as a
  visible line, not behind an `Open issues page` wizard
  option. The user can verify the result with one click; the
  wizard only collects what to *do* about a confirmed fetch
  failure (Mark unchecked / Set token / Abort) — not "go look
  at the page". Sonar's per-issue Walk wizard already prints
  a per-issue link in the preamble (§4) — the page-level
  link in §3 is the queue-level counterpart.
- **Sonar's `Accept` label is intentional, not a bug.** Bot
  review queues use `Fix` for every positive action because
  every positive action ultimately produces a fix; Sonar's
  `Accept` denotes adding a NOSONAR-style suppression — a
  deliberate acceptance of a rule violation, semantically
  distinct from a fix. Do not normalise the two.
- **Never push between phases.** Phase B uses `commit-from-fix.md`
  with `push=no`; Phase D is the single place a `git push`
  happens. Pushing inside Phase B would land the commit before
  any queued Sonar MCP `change_issue_status` calls fire and
  reorder the queue's effect on the dashboard.
