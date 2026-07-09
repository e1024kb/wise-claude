---
name: wise-feedback
description: >-
  Open a GitHub issue in e1024kb/wise-claude reporting
  feedback, a bug, or a suggestion about a wise skill or workflow — the
  submitter's environment (OS, Claude Code version, gh version, the
  cwd's public git remote as org/repo) is auto-captured and the
  Problem / Summary / Proposal sections are drafted from the user's
  prompt and the live session's signal about the misbehaving component.
  Assigns to `e1024kb` and labels `feedback`. Invoked as `/wise-feedback`
  (bare alias) or `/wise:wise-feedback` (canonical). Use when the user
  says "feedback", "report a bug", "file an issue", "this skill did a
  bad job", "the workflow got stuck", "something's wrong with /wise-*",
  "I have a suggestion", "improvement idea", "open an issue against the
  marketplace", or types `/wise-feedback`.
argument-hint: "[<feedback-text>]"
model: opus
effort: low
allowed-tools: Bash(gh:*), Bash(git:*), Bash(uname:*), Bash(sw_vers:*), Bash(claude:*), Bash(command:*), Bash(mktemp:*), Bash(printf:*), Bash(rm:*), AskUserQuestion
---

# /wise-feedback — file a feedback issue against the marketplace

## Why this skill exists

The wise plugin ships dozens of skills and workflows that users
exercise many times a day. When one of them misbehaves — a workflow
gets stuck, a skill returns a surprising result, a prompt is thin —
the friction of "switch to browser, open
github.com/e1024kb/wise-claude, click New Issue, fill three
sections, remember to assign and label" is enough to swallow most of
the feedback silently.

This skill closes the gap. A single `/wise-feedback` (optionally with
a short free-form note) captures the environment, drafts the issue
body, previews it, and creates the issue via `gh issue create` —
already assigned to [@e1024kb](https://github.com/e1024kb) and
tagged `feedback`.

### Design principle — feedback is about the plugin, not the project

The single most important framing rule for this skill: **the issue
subject is the wise plugin, not the user's project.** The user is
reporting that a `/wise-*` skill or workflow needs to be improved or
fixed; what they happened to be doing in their own codebase when the
problem surfaced is incidental.

In practice that means:

- The **prompt** and the **session signal** the skill draws from
  should describe the misbehaving wise component (which skill,
  which step, what it did wrong, what was expected), not the
  user's task at the time.
- The **Environment** section keeps one project breadcrumb — the
  public git remote URL of the cwd, rendered as `org/repo` — so
  the triager knows which downstream repo surfaced the issue. It
  does NOT carry the branch, the short SHA, the file paths, the
  cwd, or anything else that describes in-flight work.
- When the live transcript contains project-specific detail
  (filenames, branch names, ticket keys, paths), translate it into
  abstract symptoms before it reaches the issue body — e.g.
  "the skill aborted mid-edit on a TypeScript file" instead of
  "the skill aborted while editing src/components/Foo/Foo.tsx:42".

If you find yourself writing prose about the user's app, refactor
project, ticket, or branch into the issue body, stop — that's
out of scope. The triager wants signal about the plugin.

## Arguments

Claude Code hands this skill the raw argument string as `$ARGUMENTS`.
Unlike most wise action skills, this skill does **not** split on
whitespace — the entire argument string is treated as one free-form
feedback note. That matches how users will actually type:
`/wise-feedback the pr-watch loop never exits when lint keeps failing`.

| Form                       | Meaning                                                              |
|----------------------------|----------------------------------------------------------------------|
| _(empty)_                  | No prompt — skill reaches for session context alone (see §4).       |
| `<any free-form text>`     | Used as the user's feedback note; combined with session context.    |

## Procedure

### 1. Parse `$ARGUMENTS`

Trim leading/trailing whitespace. Call the result `NOTE`. An empty
`NOTE` is legitimate — the skill will try to work from session
context alone (step 4). There is no positional splitting; `NOTE` is
always the full remaining string.

### 2. Preflight — `gh` must be installed and authenticated

```bash
command -v gh
gh auth status
```

- `command -v gh` non-zero → stop with:
  ```
  /wise-feedback needs the GitHub CLI. Install it:
    brew install gh
  Then run `gh auth login` and re-invoke /wise-feedback.
  ```
- `gh auth status` non-zero → stop with:
  ```
  GitHub CLI is installed but not authenticated. Run:
    gh auth login
  then re-invoke /wise-feedback.
  ```

Never try to run `gh auth login` from the skill — it is an
interactive browser flow and will hang the session.

### 3. Capture environment context

Run each probe and remember its output. Probe failures are not
fatal — record `unknown` for that field and keep going, because the
issue should still be filed.

```bash
uname -s -r -m                       # e.g. Darwin 25.3.0 arm64
sw_vers -productName 2>/dev/null     # e.g. macOS (macOS-only)
sw_vers -productVersion 2>/dev/null  # e.g. 14.6
claude --version 2>/dev/null         # e.g. 2.0.32 (Claude Code)
gh --version                         # first line only
git remote get-url origin 2>/dev/null  # e.g. https://github.com/owner/repo.git
```

The git remote probe is best-effort — if the cwd is not inside a git
working tree, or the tree has no `origin`, omit the **Repo** line
from the Environment section entirely. Do **not** fall back to
`basename "$PWD"` or any other cwd-derived label; the breadcrumb is
useful precisely because it's a public, shareable repo URL, and a
local directory name is neither.

Derive a compact `org/repo` label from the URL for display
(e.g. `https://github.com/owner/repo.git`
→ `owner/repo`). If the remote is an SSH
URL (`git@github.com:owner/repo.git`), normalise the
same way.

Do **not** probe the branch, the short SHA, or the cwd — those
identify the user's in-progress work, which is out of scope per the
design principle above.

### 4. Derive session context from this conversation

Reflect on the conversation you — the model executing this skill —
are running inside. You can see the full transcript up to this
point. Look for signals that belong in a feedback report:

- **Skill or workflow failures** — exit codes, error messages,
  tracebacks, `BOOTSTRAP:need-python` / `WORKFLOW:failed` /
  `INIT:not-ok` / similar tokens in recent tool output.
- **Stuck loops** — the same step repeated with no progress, a
  `watch-pipelines` iteration that hit the safety break, retries
  that didn't converge.
- **User corrections** — phrases like "no, that's wrong", "stop
  doing that", "don't do X", or the user taking over manually after
  the skill gave up. These mark skill/prompt quality issues.
- **Recent slash-command invocations** — which `/wise-*` command ran
  last, and whether it returned an error.
- **"This is what prompted feedback" turns** — if the user typed
  `/wise-feedback` right after a failed run, the prior turn is almost
  certainly the subject.

Apply the design principle as you collect signal: keep what
describes the misbehaving wise component, drop what describes
the user's in-progress work. The Environment section already
carries the public repo URL — that's the only project breadcrumb
this skill ships. Don't pull in the branch, the short SHA, file
paths, cwd, or specific filenames, even when those details show up
in the transcript.

If `NOTE` is empty **and** you find no usable session signal — no
recent errors, no failed skill invocations, nothing the user is
plausibly reacting to — stop with the guidance below. Do **not**
file an empty issue.

```
/wise-feedback didn't find anything to report — no prompt and no
recent errors in this session. To file useful feedback, describe
what went wrong or what you'd like improved:

  /wise-feedback <short description>

Example:

  /wise-feedback /wise-pr-watch never exits when sonar stays red after
  three autofix attempts — it just keeps looping.
```

### 5. Draft the three sections

Synthesise **Summary**, **Problem**, and **Proposal** from `NOTE`
plus the session context you pulled in step 4. Stay grounded —
never invent failure modes, error messages, or reproduction steps
the user did not experience. If a section would be empty or pure
speculation, write `None provided.` rather than padding.

- **Summary** — one line, 60–80 chars. Becomes the issue title
  (prefixed with `Feedback: `). Focus on the *subject*, not the
  verdict:
  - Good: `/wise-pr-watch loops forever on sonar failures`
  - Good: `/wise-commit-message picks 'chore' for clear feature diffs`
  - Bad: `bug`, `improvement`, `pr-watch is bad`
- **Problem** — 2–5 sentences describing what happened, what the
  user expected, and any reproduction cues visible in the session.
  Name the concrete skill / workflow involved. Paste short
  relevant error excerpts verbatim (≤10 lines) inside a fenced
  code block when they exist; trim longer ones with a
  `[...truncated]` marker. If an excerpt carries project-specific
  detail (paths, branch names, ticket keys), redact it before
  including — keep the part that shows the wise skill's
  failure mode, drop the part that identifies the user's work.
- **Proposal** — the user's suggested fix if they gave one, or
  `None provided — open to suggestions.` otherwise. If the session
  itself surfaced a plausible fix (a config knob, a missing guard
  clause the user almost reached for), mention it as a starting
  point but attribute the observation to the skill, not to the
  user.

### 6. Assemble the full issue body

Produce this exact shape (Markdown). The trailing `Environment` and
`Session signal` subsections carry the tech context the triager
needs; never attach the full transcript.

```markdown
## Summary

<one-sentence subject>

## Problem

<2–5 sentences + optional fenced error excerpt>

## Proposal

<user suggestion or "None provided — open to suggestions.">

---

### Environment

- **OS**: <e.g. macOS 14.6 (Darwin 25.3.0 arm64)>
- **Claude Code**: <e.g. 2.0.32>
- **gh CLI**: <e.g. gh version 2.50.0>
- **Repo**: <e.g. `owner/repo`>
  <or omit this bullet if the cwd has no detectable git remote>

### Session signal

<one short paragraph naming the wise skill/workflow/step the
feedback is tied to, and what the model inferred from the transcript.
The repo URL already lives in Environment above — do NOT repeat it
here, and do NOT add the branch, short SHA, file paths, or cwd.
Omit the entire subsection if no session signal was found.>
```

Show the assembled body back to the user before step 7 so they can
read what's about to be filed.

### 7. Confirm before opening the issue

Use `AskUserQuestion`:

- Question: `Open this feedback issue against e1024kb/wise-claude?`
- Header: `Submit feedback`
- Options:
  - `Submit` — default; proceed to step 8.
  - `Edit the draft` — the user supplies a revised `NOTE` or
    tweaks; loop back to step 5 with the new input.
  - `Cancel` — stop the skill; no issue is created.

Never create the issue silently. Issue creation is visible to
everyone watching the repo — the assignee gets a GitHub notification,
the issue lands in the repo's issue list immediately, and the URL is
shareable. A typo'd `/wise-feedback` must never accidentally spam
the marketplace issue tracker.

### 8. Create the issue via `gh`

Write the body to a temp file first so multi-line content survives
without shell quoting games:

```bash
TMPFILE="$(mktemp -t wise-feedback.XXXXXX.md)"
printf '%s' "<body>" > "$TMPFILE"

gh issue create \
  --repo e1024kb/wise-claude \
  --title "Feedback: <summary>" \
  --body-file "$TMPFILE" \
  --assignee e1024kb \
  --label feedback

rm -f "$TMPFILE"
```

If the `feedback` label does not exist in the repo, `gh` errors with
`could not add label: 'feedback' not found`. Retry once without the
label and mention the miss at the end:

```bash
gh issue create \
  --repo e1024kb/wise-claude \
  --title "Feedback: <summary>" \
  --body-file "$TMPFILE" \
  --assignee e1024kb
```

Then print one line telling the user the `feedback` label is
missing from the repo and should be created via the GitHub UI so
future `/wise-feedback` runs can tag correctly.

Always remove the temp file on exit, success or failure.

### 9. Print the issue URL

`gh issue create` prints the new issue's URL on stdout. Relay it
verbatim and stop:

```
Filed: https://github.com/e1024kb/wise-claude/issues/<n>
```

## Guardrails

- **Never file without explicit confirmation.** Step 7 is the gate;
  a typo'd invocation must never accidentally open an issue.
- **Never invent errors or reproduction steps.** If the session gave
  you nothing concrete, say so. A vague "something felt off" issue
  with a fabricated traceback is worse than no issue at all — it
  wastes the triager's time and misleads future readers.
- **Feedback is about the plugin, not the user's project.** Per the
  design principle at the top of this skill: the issue body
  describes which wise skill or workflow misbehaved and how —
  not what the user was doing in their own codebase at the time.
  The only project breadcrumb that ships is the public git remote
  URL (`org/repo`) in the Environment section, because it's a
  stable, shareable identifier the triager can use to reproduce
  against. Branch, short SHA, file paths, cwd, and filenames stay
  out — redact them from any pasted error excerpt before the body
  is assembled.
- **Never attach transcripts, logs, tokens, or paths under `$HOME`.**
  The Environment section lists versions and a public git remote URL
  only — no credentials, no `~/.claude/**` content, no full cwd.
  The user can attach extras via the GitHub UI after the fact if
  they want.
- **Never assign to anyone other than `e1024kb`.** The assignee is
  fixed by the plugin's feedback-triage policy — if you want to
  change it, bump the skill, don't silently substitute someone else.
- **Never add labels other than `feedback`.** Categorisation
  (`bug`, `enhancement`, priority) is the triager's job after
  reading the issue, not the reporter's.
- **Never modify git state** and never invoke another wise action
  skill. This skill's only outward effect is one `gh issue create`
  call plus the temp-file I/O that supports it.
- **Never run `gh auth login`.** It is an interactive browser flow;
  the skill surfaces the instruction and exits instead.
