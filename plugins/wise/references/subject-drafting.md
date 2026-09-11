# subject-drafting — ticket scope + type + Conventional-Commits subject

Single source of truth for how the plugin drafts a one-line
Conventional-Commits subject. Read by:

- `wise-commit/commit-routine.md` §5–§7 (`/wise-commit`,
  `/wise-commit-push`, and `commit-from-fix.md`).
- `wise-commit-message/SKILL.md` (the read-only drafter).
- `references/pr/draft-body.md` §3 (PR-body scope).

**The caller has already collected the diff** it wants described —
staged (`git diff --cached`), pending (`git diff HEAD`), or a PR range
(`<base>...HEAD`). This reference operates on that change set; it does
not collect a diff itself, stage anything, or commit. Wherever it says
"the diff", read it as the caller's change set.

## 1. Detect a ticket reference — mandatory, never skipped

Always walk this chain before drafting. The four sources are
best-effort each, but running the step is **not** optional — skipping
it breaks tracker automation (the ticket misses commit linkage; PR
templates, release notes, and the `wise-pr-*` skills all key
off the scope). Use the tracker's actual reference format. Examples include
project keys such as `PROJ-77777`,
repository-qualified issue references such as `owner/repo#42`, numeric issue
numbers, and other tracker IDs are supported when their tracker/project identity
is established. A project-key regex is one recognition aid, not a tracker filter.
Do not treat an arbitrary number, commit hash, or version as an issue reference.
Search in order, stop at the first unambiguous match:

1. **Current branch** (`git rev-parse --abbrev-ref HEAD`) — the
   load-bearing default; covers the common case
   (`feat/PROJ-123-foo`, `PROJ-123-foo`, `bugfix/PROJ-123`). Preserve the
   tracker's canonical reference and case.
2. **Diff content** - inspect explicit ticket references and URLs in the change
   set (`@ticket`, issue links, TODO references, fixtures). Match the established
   tracker's format, not only project-acronym keys; use this source only when
   the branch yielded nothing.
3. **Recent commit subjects** (`git log -5 --pretty=%s`) — useful when
   the branch is `main` but commits stack under one ticket.
4. **The current host session** — a reference or ticket URL the user
   mentioned earlier. Last resort, after the three git sources.

If nothing matches, the subject is **unscoped** — that is a valid
output. **Never invent a key, never borrow one from an unrelated place
(README, open tab, template default), never prompt for one.**
Wrong-scoped is worse than unscoped: it pollutes the wrong ticket's
activity log. Preserve repository/project identity for otherwise ambiguous numeric
IDs. If a reference cannot safely fit a commit scope, use an unscoped subject and
retain its verified URL in the body rather than rewriting it into a made-up key.
For links, use the exact URL supplied by the user, run context, or tracker response.
Never construct a tracker hostname or URL from an identifier alone.

**Self-check before drafting:** state which source matched and what it
returned. A found reference suitable for a Conventional Commits scope appears as
`<type>(<REF>): …`; "no ticket reference found" means the unscoped `<type>: …` form. Drafting without walking
the chain — even on a "this is obviously a small fix" rationalisation —
is the bug this step exists to prevent.

## 2. Classify the change

Read the diff content for context. Pick a single Conventional-Commits
type — first row that fits wins:

| Type       | Fits when the diff…                                              |
|------------|------------------------------------------------------------------|
| `feat`     | adds new user-visible behaviour or a new API                     |
| `fix`      | fixes a bug (new guard clause, reverted logic, "fix" keyword)    |
| `refactor` | restructures code without behaviour change                       |
| `perf`     | changes primarily to make something faster / less wasteful       |
| `test`     | only touches `tests/`, `__tests__/`, `*.test.*`, `*.spec.*`      |
| `docs`     | only touches `*.md`, `docs/**`, code comments, or docstrings     |
| `style`    | whitespace / formatting only, no logic change                    |
| `build`    | build config, dependency bumps, lockfiles, Dockerfile, Makefile  |
| `ci`       | only touches `.github/workflows/**` or CI config                 |
| `chore`    | everything else that doesn't change source behaviour             |

For mixed diffs, pick the dominant change. Tied calls go to the
stronger semantic: `feat` > `fix` > `refactor` > `perf` > everything
else.

## 3. Write the subject

Format:

- With ticket reference: `<type>(<REF>): <subject>`
- Without:       `<type>: <subject>`

Subject rules:

- One line, imperative mood ("add", "fix" — not "added", "fixes").
- Lowercase first letter after the colon unless it's a proper noun.
- No trailing period.
- Aim ≤72 chars total; hard wrap at 100.
- Summarise the dominant change; don't enumerate files.
- **No `Co-Authored-By` trailer, no `🤖 Generated` footer, no
  Anthropic / Claude attribution.** The commit is the user's; this is
  drafting prose, not claiming authorship.
