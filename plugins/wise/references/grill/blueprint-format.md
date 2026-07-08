# grill/blueprint-format — the BLUEPRINT-<ref>.md schema

Single source of truth for the **blueprint** artifact — what wise
writes when a researched ticket is **not** plannable yet
(`gap-analysis.md` verdict `GAPS`). A blueprint is the normalized,
evidence-backed understanding of the ticket **plus** the targeted
questions that close the remaining gaps; once answered, it upgrades
into a regular `PLAN-<ref>.md`. Read by:

- `skills/wise-grill/SKILL.md` §6 — writes and later upgrades it.
- `workflows/ticket-plan/workflow.yaml` `gap-analysis` step — writes it
  when the run pauses on gaps.
- `workflows/ticket-auto/prompts/plan-ticket.md` §6 — writes it beside
  the blocked verdict when a ticket is autonomously unplannable.

## File naming and location

`BLUEPRINT-<ref>.md`, where `<ref>` is the ticket ref with any leading
`#` stripped (`BLUEPRINT-PROJ-777.md`, `BLUEPRINT-678.md`). It lives
wherever the caller keeps its plans — `docs/plans/` for the standalone
skill, `{{run.dir}}/plans/` for a workflow run — always beside the
`PLAN-<ref>.md` it will become.

## Schema

```markdown
# BLUEPRINT <tracker>:<ref> — <Title>

> **Status:** AWAITING-ANSWERS (<n> open questions for <m> people)
> **Source:** <ticket url> · researched <date> · HEAD <short-sha>

## What this ticket actually means
One-to-three plain-language paragraphs restating the ticket as the
research understood it — the normalized version a newcomer could act
on. No jargon that the Lexicon below doesn't resolve.

## Goal
The user / business problem this solves, evidence-backed (quote +
source). If the goal itself is a gap, say so here in one line and
point at the question that covers it.

## Known facts
| fact | source |
Each row one researched finding that the plan will rely on.

## Lexicon
| term | meaning | source |
Only load-bearing terms. UNRESOLVED terms appear here too, flagged,
each covered by a question below.

## Assumptions (proceeding on these unless corrected)
| assumption | confidence | basis |
The ASSUMED dimensions from the gap analysis, verbatim. Answering the
questions below may overturn these — that is the point.

## Questions
### → <Name or role> (<why them>)
- [ ] **Q1 — <one-line question, recommended answer first>**
      Context: <what the research already found, with source>
      Default if unanswered: <the assumption the plan will proceed on>
      Answer: _pending_
(One `### →` block per addressee, ordered by blocking impact;
questions follow the crafting rules in `gap-analysis.md` §3.)

## Clarifications log
### Session <date>
| question | answer | answered by |
Appended on every upgrade pass — the durable record of what was
decided, so a re-run never re-asks a settled question.

## Draft plan skeleton
The best-effort shape of the eventual PLAN — Summary, likely task
waves, testing surface — with `⚠ blocked by Q<n>` markers on every
part a pending answer could change. Enough that the answerer sees
what their answer steers.

## Sources
Consulted: <channel → what was searched / read>
Unavailable: <channel → why → what it likely holds>

## Next step
Fill the `Answer:` lines (or reply to the questions wherever they were
asked) and re-run `/wise-grill <ticket>` — it ingests the answers,
re-scores the gaps, and upgrades this blueprint into `PLAN-<ref>.md`.
```

## Rules

- **Self-contained.** A person who never saw the session must be able
  to read the blueprint, answer the questions, and understand what
  happens next. No "as discussed above".
- **Answers are written back in place.** An upgrade pass fills the
  `Answer:` line, ticks the checkbox, appends the Clarifications-log
  row, and folds the answer into the affected sections — it never
  duplicates the old statement next to the new one.
- **Settled questions never resurface.** A question with a non-pending
  `Answer:` (or a Clarifications-log row) is closed; a re-run may add
  NEW questions only if the answers themselves opened a real gap.
- **The blueprint is the handoff artifact.** The per-person `### →`
  blocks are written to be pasted into Slack / a comment verbatim —
  context line, options, default and all.
- **Status flips, file upgrades.** When every critical gap is closed,
  the caller writes `PLAN-<ref>.md` (wise's regular plan schema) beside
  the blueprint and flips the blueprint's status line to
  `RESOLVED → see PLAN-<ref>.md`. The blueprint stays as the decision
  record; the plan is what gets implemented.
