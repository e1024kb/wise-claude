---
name: wise-ste
description: >-
  Rewrite a draft, a file, or your last response in ASD-STE100
  Simplified Technical English, with short sentences, active voice,
  one instruction per sentence and one term per thing, and return the
  result without posting it anywhere. Invoked as `/wise-ste` (bare
  alias) or `/wise:wise-ste` (canonical). Use when the user says
  "ste", "rewrite in STE100", "simplified technical english", "make
  this STE", "simplify this text", or types `/wise-ste`.
argument-hint: "[<draft text, file path, or pointer>]"
allowed-tools:
  - Read
---

# /wise-ste - rewrite text in ASD-STE100

Before executing, follow [model fallback](../../references/workflow-host-control.md#model-fallback)
for unavailable models or delegation routes, including in autonomous procedures.

At every skill start, identify your main/child role and the current client
and GUI/TUI question tools, then read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions in autonomous or otherwise prompt-free procedures.

## Why this skill exists

Text from a work session is often long, passive, and full of changing
terms. ASD-STE100 makes text easy to read for every reader, including
readers whose first language is not English. This command applies the
shared [ASD-STE100 rules](../../references/ste100.md) to one text on
request. The wise session hook applies the same rules automatically.

## Arguments

Parse `$ARGUMENTS` as one free-form value.

| Input | Behaviour |
|---|---|
| Draft text inline | Rewrite the text. Return the result. |
| A file path | `Read` the file. Rewrite its content. Return the result. Do not change the file. |
| A pointer ("the PR body above", "my last comment") | Find that text in the conversation. Rewrite it. Return the result. |
| _(empty)_ | Rewrite your last response in this conversation. If there is no response, ask for the text to rewrite. |

A file path that does not exist is an error. Report it and stop. Do not
ask for a different input.

## Procedure

1. Parse `$ARGUMENTS` per the table above and find the source text.
2. Read [`ste100.md`](../../references/ste100.md).
3. Rewrite the prose of the source text per those rules.
4. Keep code blocks, commands, paths, identifiers, numbers, URLs, and
   quoted errors verbatim.
5. Keep each fact, number, failure, and reference code (`F1`, `A2`,
   and so on) from the source text.
6. Keep the markup of the target surface. If the target is known, also
   apply [`wise-human-writing`](../wise-human-writing/SKILL.md) for
   structure and markup.
7. Return only the rewritten text.

## Guardrails

- This command never posts, sends, or submits text. It only returns
  text.
- This command never edits a file.
- Accuracy is more important than style. Do not remove a fact to make
  a sentence shorter.
