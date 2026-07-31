---
name: wise-human-writing
description: >-
  Human-first writing rules for EVERY outbound message composed on the
  user's behalf — ticket descriptions and comments in ANY issue tracker
  (Jira, Linear, GitHub Issues, GitLab, Asana, Monday, YouTrack, …),
  PR / MR descriptions, comments, and review replies on ANY code host
  (GitHub, GitLab, Bitbucket, …), pages and specs in ANY doc system
  (Confluence, Notion, Google Docs, …), and chat messages (Slack,
  Teams, Discord, …). ALWAYS consult this skill before drafting or
  posting any such content, whichever MCP server, CLI, or API is in
  play: structure every message as context → reasoning → conclusion,
  write at business level instead of dumping code identifiers, never
  use ASCII diagrams, and emit the correct native markup per tool.
  Also invocable as `/wise-human-writing [<draft>]` (bare alias) or
  `/wise:wise-human-writing` (canonical) to rewrite — "de-slop" — an
  existing draft without posting it anywhere. Use when creating or
  updating a ticket, commenting on an issue or PR, posting a status
  update to Slack, writing a Confluence / Notion page, or when the
  user says "de-slop this", "make this readable", "humanize this
  message", "rewrite this comment", "too much AI slop", or types
  `/wise-human-writing`.
argument-hint: "[<draft text or file/ref to rewrite>]"
allowed-tools:
  - Read
---

# wise-human-writing — write for humans, not for logs

## Why this skill exists

Messages generated straight from a work session read like the work
session: exhaustive, symbol-heavy, structureless. The reader of a Jira
comment or a Slack update was not in that session. They need the
*story* — what happened, why, what's next — in the order a human tells
it, at the altitude a human cares about. This skill is the standing
contract for every outbound message: it defines the structure, the
altitude, and the per-tool formatting so that anything posted on the
user's behalf reads like a sharp colleague wrote it.

## Two ways this skill runs

1. **Standing reference (the main mode).** Consult these rules
   automatically whenever composing content that will land in front of
   other humans through an external tool: ticket descriptions, tracker
   comments, PR/MR descriptions or comment replies, doc pages, chat
   messages. It does not matter which tracker, code host, doc system,
   or chat product is in play, or whether the write happens through an
   MCP server, a CLI (`gh`, `glab`, `jira`, `linear`), or a raw API
   call — if a human will read it in another tool, these rules apply.
2. **Slash rewrite** — `/wise-human-writing [<draft>]`. The user
   pastes a draft (or points at a file / a message earlier in the
   conversation) and gets back a cleaned version. This form **never
   posts anywhere** — it returns text and stops.

## Arguments (slash form only)

Parse `$ARGUMENTS` as one free-form value:

| Input | Behaviour |
|---|---|
| Draft text inline | Rewrite it per the rules below; return the result. |
| A file path | `Read` the file, rewrite its content, return the result. Do not modify the file. |
| A pointer ("the comment above", "my last Slack draft") | Locate that text in the conversation, rewrite, return. |
| _(empty)_ | If an outbound message is currently being drafted, apply the rules to it. Otherwise summarize the rules in three lines and ask what to rewrite. |

When rewriting, also name the target surface if it is known or
guessable ("this reads like a Jira comment — formatted for Jira") so
the markup matches where the text will land (see the matrix below).

## The rules

### 1. Golden structure — context → reasoning → conclusion

Every message tells its story in this order:

1. **Context / cause** — why this message exists. One or two
   sentences: the problem, the trigger, the question being answered.
2. **Reasoning / findings** — the short version of what was learned
   or decided, and why. Not the journey; the takeaways.
3. **Conclusion / next step** — what happens now, who is expected to
   do what, or what decision is needed.

The test: a reader skims it in ~20 seconds, understands the state of
the world, and can reply to a specific part ("re: your second point").
If a message can't be skimmed that fast, it's structured wrong, not
too short.

### 2. Altitude rule — business language first

Write conceptually, at the level of what the system *does*, not how
the code spells it. "The retry logic gave up too early" — not
"`retryWithBackoff()` hit `MAX_ATTEMPTS=3`". The message must stay
conceptually correct and accurate at that altitude; simplifying the
language never licenses simplifying the truth.

Include technical detail only when:

- the reader **explicitly asked** for depth, or
- the point **cannot be made** without it — dropping the detail would
  lose the core sense.

When technical detail is warranted, keep it earning its place:
concise, precise, in a list or table, with a link (PR, commit, run,
dashboard) for anyone who wants the full depth — never inline dumps
of code, stack traces, or config. A couple of purposeful links beat
ten exhaustive ones — and every link is clickable and labeled, in
the target tool's native syntax (see the links rule under the
formatting matrix).

### 3. Anti-slop checklist

Self-apply before posting. Each of these is a tell that the text was
generated, and each one costs the reader time:

- No achievement openers — "I've successfully implemented…",
  "Great news!…". Start with the substance.
- No file-by-file change enumeration. Readers who need the diff will
  open the link; the message says what changed *conceptually*.
- No restating the ticket / question back at the reader — they wrote
  it.
- No hedge padding ("it seems", "likely", "should probably") unless
  the uncertainty is real and worth flagging — then flag it once,
  explicitly.
- No emoji walls, no bold-everything. Bold marks the one thing that
  must not be missed, or nothing.
- Short paragraphs — three to four lines each, one idea per
  paragraph.
- Be brief and structured. There is no word cap; there is a
  relevance cap — everything present must change what the reader
  knows or does.

### 4. No ASCII art — sequential prose instead

Never use ASCII diagrams, box-drawing flow charts, or ASCII trees.
They render differently (often brokenly) across trackers, chat
clients, email, and mobile, and they can't be quoted or referenced in
a reply.

A long flow is expressed as short numbered steps — one sentence each,
in sequence, in human language:

1. The importer receives the file and queues it.
2. A worker validates the rows and rejects the malformed ones.
3. Valid rows land in the staging table for review.

Now a reply can say "step 2 is where it breaks" — which is exactly
what diagrams were supposed to enable and mostly don't.

### 5. Per-surface templates

Openers for the common message types. Use them as the default shape,
not as mandatory headings — a two-sentence comment doesn't need
section labels; long content benefits from them.

- **Ticket description:** Problem → Why it matters → Scope (in / out)
  → Acceptance criteria → Links.
- **Bug ticket:** What happens → Impact (who / how much) → Cause (if
  known) → Proposed fix → Links.
- **Status comment:** Done → Key decision and why → Next.
- **Investigation comment:** Cause → How we found it (2–3 sentences)
  → Conclusion, or the decision now needed and from whom.
- **Chat message (Slack etc.):** First line is the TL;DR — readable
  alone in a notification. Then 2–4 bullets, at most one link.
  Detail goes to the thread, not the channel.

### 6. Formatting matrix — emit the tool's native markup

Wrong markup is its own kind of slop: markdown tables posted into
Slack, `##` headings in Jira. Match the target:

| Surface | Markup | Tables | Collapsible | Notes |
|---|---|---|---|---|
| Jira | Wiki markup / ADF | Yes | No `<details>` | `{code}` blocks, `h2.` headings, Jira link syntax |
| GitHub / GitLab / Linear | Markdown | Yes | `<details>` works | Standard GFM |
| Slack | mrkdwn | **No** — bullet lines instead | No | No headings; `*bold*` single-asterisk; keep it flat |
| Confluence / Notion / Google Docs | Rich text via their APIs | Yes | Varies | Headings + bullets fine; keep hierarchy flat (two levels) |

When the target tool is unknown, write plain flat text with bullets —
it degrades gracefully everywhere.

**Links must be clickable.** A bare URL pasted as plain text often
renders dead — Jira and Slack in particular won't always auto-link
it, and a raw 90-character URL is unreadable even when they do.
Always emit the target surface's link syntax with a short human
label:

- GitHub / GitLab / Linear (Markdown): `[the fix PR](https://…)`
- Jira wiki markup: `[the fix PR|https://…]`
- Slack mrkdwn: `<https://…|the fix PR>`
- Doc systems: use the API's rich-text link construct; if none is
  available, put the bare URL on its own line — never glued
  mid-sentence to punctuation, which breaks auto-linking.

The label says what's behind the link ("the fix PR", "run #123",
"the design doc") — never "click here", never the naked URL as its
own text.

**Links are always clickable, labeled, and native.** A bare pasted URL
(`https://github.com/org/repo/pull/123`) is a formatting bug: several
tools don't auto-link it, and even when they do, the raw address tells
the reader nothing. Every link gets descriptive text ("the PR", "the
incident timeline", "PROJ-123") in the target tool's own link syntax:

- Jira wiki markup — `[the PR|https://…]`
- Markdown (GitHub / GitLab / Linear) — `[the PR](https://…)`
- Slack mrkdwn — `<https://…|the PR>`
- Confluence / Notion / Google Docs — a real hyperlink via the API's
  link field, never a URL pasted as plain text

Never use "click here" / "link" as the text, and never show the raw
URL as its own label unless the URL itself is the information (a
webhook endpoint someone must copy).

### 7. Audience dial

The user can shift the altitude explicitly: "for engineers" moves the
allowed technical depth up one notch (identifiers and precise
internals become acceptable where they help); "for stakeholders"
moves it down (outcome and impact only, zero internals). Absent a
dial, default to the mixed audience of rule 2.

### 8. Collapsible technical appendix

Where the surface supports it (GitHub, GitLab, Linear), serve both
audiences at once: the business-level message on top, and an optional
`<details><summary>Technical notes</summary>…</details>` block below
carrying the identifiers, commands, and precise internals. The casual
reader never scrolls past noise; the engineer clicks once.

On surfaces without collapsibles (Jira, Slack), the appendix becomes
a link to where the depth already lives (the PR, the run, the doc) —
not an inline dump.

## Example — before and after

**Before** (typical generated status comment):

> I've successfully implemented the caching layer improvements! ✅ The
> changes modify `CacheManager.ts`, `RedisAdapter.ts`,
> `cache.config.json`, and 7 test files. First I analyzed the existing
> TTL handling in `getWithFallback()`, then I refactored
> `invalidateByPrefix()` to use `SCAN` instead of `KEYS`, and
> updated `MAX_PIPELINE_SIZE` from 100 to 512…

**After** (same facts, human-first):

> Cache invalidation was blocking production reads for seconds at a
> time — that's what caused the checkout slowdowns reported last week.
>
> The invalidation routine was scanning the entire cache in one
> blocking pass. It now walks it incrementally, so reads keep flowing
> during cleanup. Verified against a production-sized dataset: worst
> observed pause went from ~4 s to under 50 ms.
>
> Next: rolling out behind the existing cache flag this week. Details
> in the PR: [link].

## Guardrails

- **This skill never posts, sends, or submits anything.** It shapes
  text. The posting is done by whatever flow or tool was already
  doing it — and outbound sends remain subject to their own
  confirmation rules.
- **Accuracy outranks polish.** Never drop a fact, soften a failure,
  or round off a number to make the message read better. If tests
  failed, the human-first version still says tests failed.
- **The slash form is read-only.** It may `Read` a file the user
  points at; it never edits files, never calls tracker / chat tools.
- **Don't over-template.** A one-line answer to a one-line question
  is already human-first; forcing three sections onto it is its own
  kind of slop.
