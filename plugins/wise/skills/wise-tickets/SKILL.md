---
name: wise-tickets
description: >-
  Ticket-writing rules for EVERY ticket composed on the user's behalf
  in ANY issue tracker (Jira, Linear, GitHub Issues, GitLab, Asana, …)
  or tracker-like system.
  ALWAYS consult this skill before creating, editing, updating,
  splitting, or breaking down a ticket or epic, whichever MCP server,
  CLI, or API is in play: a ticket is concise
  and human-readable, never an implementation plan; it follows the
  canonical sections (Summary, Goal, Scope, Acceptance criteria,
  References, Labels, Estimate); large work
  splits per repository/service with sensible aggregation; estimates
  defer to the wise-estimation scale. Also invocable as
  `/wise-tickets [<ticket-ref or draft>]` (canonical
  `/wise:wise-tickets`) to restructure an oversized ticket
  or draft and, when asked, apply it back to the tracker. Use when
  creating or updating a ticket or issue, breaking an epic into
  tickets, or when the user says "create a ticket", "file an issue",
  "break this down into tickets", "this ticket is too big", or types
  `/wise-tickets`.
argument-hint: "[<ticket-ref or draft>]"
---

# wise-tickets - tickets that read like tickets, not plans

## Why this skill exists

A ticket generated straight from a work session reads like the work
session: an exhaustive implementation plan with file lists, code
snippets, and step-by-step prose. That is a Claude Code plan, not a
ticket. A ticket is a *contract between humans*: what we want, why,
where it stops, and how we know it's done - skimmable in a minute by
a PM, a reviewer, or the engineer who picks it up in three weeks.
This skill is the standing contract for every ticket written on the
user's behalf: the section shape, the altitude, the breakdown rules,
and the estimation link.

This skill composes with two siblings:

- **`wise-human-writing`** governs voice and markup - business-level
  language, no ASCII diagrams, native markup per tracker, no long
  dashes. Apply it to every sentence of the ticket.
- **`wise-estimation`** governs the per-ticket number - the Fibonacci
  0.5 → 13 SP scale and the "> 8 SP is a research ticket" rule.

`wise-tickets` governs everything those two don't: the structure,
the scope discipline, and how large work becomes several tickets.

## Two ways this skill runs

1. **Standing reference (the main mode).** Consult these rules
   automatically whenever creating, editing, updating, or splitting a
   ticket in any tracker or tracker-like system. The skill does the
   full job, reads and writes included: use whatever tracker tooling
   the session has - an MCP server (Jira, Linear, Asana, …), a CLI
   (`jira`, `linear`, `gh issue`, `glab issue`), or a raw API - to
   fetch, create, and update the tickets it shapes. When no tracker
   tooling is reachable, say so and hand back the drafted content
   instead of guessing at a write path.
2. **Slash restructure** - `/wise-tickets [<ticket-ref or draft>]`.
   The user points at an existing ticket (a key, a URL, a pasted
   draft, a file) and gets back a version restructured into the
   canonical shape below. By default this returns text and stops;
   when the user asks to apply it ("update the ticket", "apply
   this"), write the restructured version back to the tracker with
   the same tooling.

## Arguments (slash form only)

Parse `$ARGUMENTS` as one free-form value:

| Input | Behaviour |
|---|---|
| A ticket key or URL (`PROJ-123`, a Linear/GitHub issue link) | Fetch it with the session's tracker tooling; restructure; return the rewrite. Apply it back to the ticket when the user asks. If no tracker access exists, say so and ask for the text. |
| Draft text inline | Restructure it per the rules below; return the result. |
| A file path | `Read` the file, restructure its content, return the result. Do not modify the file. |
| _(empty)_ | If a ticket is currently being drafted in the conversation, apply the rules to it. Otherwise summarize the canonical shape in three lines and ask what to restructure. |

When the input is one oversized ticket that should really be several,
say so and return the proposed split (parent + children per the
breakdown rules) instead of one padded rewrite.

## The canonical ticket shape

Every ticket carries these sections, in this order. Render them in
the tracker's native markup (headings in GitHub/Linear markdown,
`h2.` in Jira wiki markup - see the `wise-human-writing` matrix).
A trivial ticket may merge sections, never skip their content.

1. **Summary** - the title plus one or two sentences of context: the
   problem or need, in business language. Not the solution.
2. **Goal** - the outcome this ticket exists to produce. One
   sentence. If the goal needs a flow, write it as short numbered
   steps in prose (per `wise-human-writing` rule 4), never a diagram.
3. **Scope** - what is being done, specifically, and an explicit
   **Out of scope** line for the adjacent work this ticket does NOT
   cover. The out-of-scope line is what prevents scope creep in
   review; write it even when it feels obvious.
4. **Acceptance criteria** - a short checklist of testable
   statements. Each one is verifiable by someone other than the
   author ("retries stop after the third failure", not "improve
   retry logic"). Three to six items; more means the scope is too big.
5. **References** - curated links, labeled and clickable in the
   tracker's native link syntax:
   - requirement sources: PRD, TRD, Release Memo, design (Figma);
   - observability: the Grafana / BetterStack dashboard or log query
     that shows the problem or will show the fix;
   - related tickets and the **parent epic/ticket** link;
   - only genuinely relevant links - three purposeful links beat ten
     exhaustive ones.
6. **Labels** - the tracker's labels/components for team, service,
   and work type, following whatever taxonomy the project already
   uses (look at neighbouring tickets; don't invent labels).
7. **Estimate** - one SP value from the `wise-estimation` scale, set
   in the tracker's estimate field when it has one, otherwise as the
   last line.

## Writing rules

- **A ticket is not an implementation plan.** No code dumps, no
  exhaustive file lists, no step-by-step "first I will, then I will"
  prose, no architecture essays. The reader needs the what and the
  why; the how at file level belongs in the PR. If a genuinely
  necessary technical note exists (a known constraint, a chosen
  approach that must not be re-litigated), it gets two or three
  sentences under Scope, or a link to the TRD.
- **Concise means selective, not compressed.** Full sentences,
  business altitude, every line changing what the reader knows.
  A good ticket body fits on one screen without scrolling.
- **Flows are numbered prose.** "1. The importer queues the file.
  2. A worker validates rows…" - so a comment can say "step 2 is
  wrong". Never ASCII art (see `wise-human-writing` rule 4).
- **Edits obey the same shape.** When updating or adjusting an
  existing ticket, preserve or introduce the canonical sections;
  don't append a changelog of prose at the bottom of an already
  shapeless description.

## Estimation - two linked rules

- **Per ticket:** size with the `wise-estimation` scale (Fibonacci
  0.5 → 13). Anything landing above 8 SP is a signal to split or to
  spike, not an estimate - see that skill's "research ticket" rule.
- **Capacity, for breakdowns:** one developer sustains roughly
  **5-8 SP per week** of throughput. When breaking a large body of
  work into tickets, target each resulting ticket at **5 SP or
  less** - the scale's ~1 week row - so one person ships it inside a
  week. This is wise-tickets' own split-sizing heuristic, not part
  of `wise-estimation` (which deliberately stays out of velocity
  planning); a sizing target, never a deadline commitment.

## Breakdown rules

When the work is too big for one ticket:

- **Split by repository/service first.** Each ticket ideally lands in
  one repo/service and names it (in the title prefix or a label,
  following the project's existing convention). One owner, one PR
  stream, one deploy surface.
- **Aggregate; don't atomize.** Tickets in the 2-5 SP range are the
  sweet spot. Ten 0.5 SP tickets are tracker noise - fold mechanical
  siblings together. The unit is "a coherent chunk one person ships",
  not "one commit". (Distinct from `wise-estimation`'s "sub-tasks
  ≤ 3 SP" decomposition rule: that governs sub-tasks *inside* one
  ticket during planning; this section governs whole tickets when
  splitting an epic.)
- **Fold prerequisite schema work into one ticket.** Protobuf
  messages, API contracts, shared types, migrations - the
  contract-first work that several tickets depend on - becomes a
  single aggregated ticket that the others link as a dependency,
  not a fragment inside each consumer's ticket.
- **Keep the family linked.** Every child links the parent
  epic/ticket; dependency order is expressed with the tracker's
  blocks/blocked-by links (or a "Depends on" line in References when
  the tracker has none). A reader opening any one ticket can reach
  the whole picture in one click.
- **The parent stays thin.** The epic/parent holds the Summary, Goal,
  and the list of children - not a duplicate of every child's scope.

## Beyond issue trackers

The same shape applies wherever a tracker-like record is created on
the user's behalf: a BetterStack incident, a design-system task, an
ops runbook item. Map the sections to whatever fields the system has
(an incident's Summary/Impact/References map one-to-one); keep the
same discipline - concise, scoped, linked, testable done-state.

## Example - before and after

**Before** (typical generated ticket, abridged):

> Implement caching layer improvements. Modify `CacheManager.ts` to
> refactor `invalidateByPrefix()` to use SCAN instead of KEYS. Step 1:
> analyze TTL handling in `getWithFallback()`. Step 2: update
> `MAX_PIPELINE_SIZE` from 100 to 512. Step 3: update 7 test files…
> [40 more lines of files and steps]

**After**:

> **Summary:** Cache invalidation blocks production reads for seconds
> at a time, causing the checkout slowdowns reported last week.
>
> **Goal:** Reads keep flowing during cache cleanup.
>
> **Scope:** Rework the invalidation routine in the cache service to
> walk the cache incrementally instead of one blocking pass. Out of
> scope: TTL policy changes, cache-size tuning.
>
> **Acceptance criteria:**
> - Worst read pause during invalidation under 100 ms on a
>   production-sized dataset.
> - Existing invalidation behaviour unchanged (same keys removed).
> - Rollout behind the existing cache flag.
>
> **References:** [PRD](…) · [checkout latency dashboard](…) ·
> parent: [PROJ-100](…)
>
> **Labels:** `cache-service`, `performance`
> **Estimate:** 3 SP

## Guardrails

- **Frontmatter deliberately omits `allowed-tools`.** The tracker
  toolset is open-ended - any tracker MCP server, CLI, or API the
  session has - so the tool names cannot be enumerated in a
  restriction list; the skill inherits the session's tools instead
  (CONTRIBUTING sanctions "narrow set or omit"). Don't "fix" this
  back to a narrow list.
- **Show before you write.** Creating or updating a ticket in an
  external tracker is an outward-facing action: present the drafted
  ticket (or the split) and get the user's go-ahead before
  submitting, unless the user already explicitly asked for the write
  in this request ("create the tickets", "update PROJ-123") or an
  autonomous flow that owns the confirmation invoked this skill.
- **Writes go through the session's tracker tooling.** MCP server,
  CLI, or API - whatever is connected. Never invent an endpoint; if
  no tooling is reachable, return the drafted content and say what's
  missing.
- **The slash form defaults to returning text.** It fetches and
  restructures; it writes back only when the user asks it to apply
  the result. It never modifies local files it was pointed at.
- **Accuracy outranks brevity.** Never drop a real constraint,
  dependency, or known risk to make the ticket shorter - compress the
  prose, not the truth.
- **Follow the project's taxonomy.** Labels, components, estimate
  fields, link types: mirror what neighbouring tickets in that
  project already use rather than imposing this skill's defaults.
