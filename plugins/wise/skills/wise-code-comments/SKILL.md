---
name: wise-code-comments
description: >-
  Code-comment rules for EVERY comment and docstring written on the
  user's behalf in ANY programming language. ALWAYS consult this
  skill before writing, rewriting, or reviewing a code comment,
  docstring, or inline annotation - whether adding comments to new
  code, editing code that carries comments, or when the user says
  "add comments", "document this function", "comment this code",
  "explain this in the code", or "clean up the comments". A comment
  MUST read like a plain-language (ELI5) statement of what the code
  does or what constraint it obeys RIGHT NOW - concise, present
  tense, no history, no ticket archaeology.
allowed-tools:
  - Read
---

# Code-comment standards

Standing contract for every code comment and docstring wise writes,
in any language. Not user-invocable - it applies automatically
whenever comments are written or edited.

## The rules (MUST)

1. **ELI5 wording.** Write so a developer new to the codebase
   understands the comment on first read. Plain words, short
   sentences, the simplest domain term that carries the fact. No
   jargon a newcomer would have to look up when a common word works.

2. **Concise.** One comment states one fact, once. If the comment can
   lose a word without losing the fact, lose the word. A comment that
   restates what the line below obviously does is deleted, not
   shortened.

3. **Present-tense facts only.** Describe what the code does or what
   constraint it obeys *now*. The comment must stay true when read in
   isolation years later by someone with no access to the project's
   history.

4. **No history.** Never explain how the code came to be: no ticket
   numbers as narrative ("in PROJ-12345 we refactored this to ..."),
   no "previously this used X", no "changed to fix the bug where
   ...", no "now correctly handles ...". History lives in commit
   messages, PR descriptions, and tickets.

5. **No author-to-reviewer talk.** A comment never justifies the
   change to whoever is reading the diff ("this is safe because we
   already validated above"). If the fact matters to every future
   reader, state the fact itself; otherwise put it in the PR.

## What a comment is for

The code shows *what* it does. A comment earns its place only when it
carries something the code cannot show:

- a constraint ("callers hold the lock before entering")
- a non-obvious reason ("the API returns 200 on partial failure, so
  check the body")
- a unit, range, or format ("timeout in milliseconds")
- a warning ("order matters: flush before close or data is lost")

## Examples

Bad (history, narration):

```go
// In ticket WISE-12345 we refactored this in order to avoid the
// race condition we had with the old cache implementation.
func loadConfig() {...}
```

Good (present-tense fact):

```go
// Reads config once and caches it; safe to call from many
// goroutines.
func loadConfig() {...}
```

Bad (diff-talk, restating the obvious):

```python
# Now correctly increments the counter (was off by one before).
counter += 1
```

Good (delete it - comment only if there is a real fact to add):

```python
counter += 1
```

Bad (jargon-dense):

```ts
// Memoized thunk hydrating the normalized entity slice idempotently.
```

Good (ELI5):

```ts
// Fills the user cache from the API. Calling it twice does nothing
// the second time.
```

## Guardrails

- Apply these rules to comments **you write or rewrite**. Do not
  sweep a file rewriting pre-existing comments unless the user asked
  for that.
- Docstrings follow the same rules; keep whatever structural
  convention the codebase already uses (JSDoc, Google style, etc.).
- Match the comment density of the surrounding code - these rules
  govern wording, not how many comments a file should have.
