# PLAN-011 — insights.py: fix retire-without-supersede no-op + ledger hardening

## Source
- Scope: plugins/wise/scripts/insights.py
- Found by: correctness + security + dx lenses (3 deduped findings) · leverage 0.9 (impact 2 ÷ effort 2 × confidence 0.9)
- Commit: e9971c5
- Evidence: insights.py:1027-1034 (retire writes decision without `superseded_by` key), :1051-1067 (`_reconcile_retired` deletes any retired decision whose `superseded_by` is not an existing skill — `None not in existing` is always True), :124-125 (`_ensure_root` — no permission tightening; ledger under ~/.local/share/wise/insights/ inherits umask, typically world-readable), :1 + file mode 644 (shebang without exec bit; siblings are 755)

## Summary
Three insights defects: (1) `retire` without `--superseded-by` writes a
`retired` decision that the very next `mine` deletes —
`_reconcile_retired` drops any retired decision whose `superseded_by`
(None here) isn't an existing skill dir, so the deliberately retired
pattern immediately resurfaces as a candidate; the suppression the
decision record exists to provide never holds. (2) The insights data
root (ledger.jsonl holds redacted-but-still-sensitive prompt history)
is created with default permissions — world-readable on multi-user
hosts. (3) insights.py carries a shebang but no exec bit — an
inconsistency trap for future direct-exec callers. Done = retired stays
retired until its cluster is genuinely superseded-and-removed, the data
root is 0700, and the file mode matches its siblings.

## Assumptions
- Intended semantics of plain `retire` (no supersede): the pattern was
  deliberately removed and should NOT resurface unless the user runs
  `restore` or resets decisions. (The alternative — "un-superseded
  retire may resurface" — would make writing the decision entry dead
  code, per the reconcile logic; docs/wise/insights.md should be checked
  for an explicit statement; absent one, this plan's reading stands.)
- ledger.jsonl / candidates.json / decisions.json all live under
  `insights_root()`; chmodding the root dir 0700 covers them
  (individual file chmod optional belt).

## Decisions Made
- Fix in `_reconcile_retired`: only reconcile entries that HAVE a
  `superseded_by` key — `dec.get("superseded_by")` present AND not in
  existing → clear; entries without the key are permanent until user
  action. One-line condition change + comment.
- `_ensure_root`: `mkdir(..., mode=0o700)` + `os.chmod(root, 0o700)`
  after (mkdir mode is masked by umask; explicit chmod guarantees it).
- `chmod 755 plugins/wise/scripts/insights.py` (keep shebang) — matches
  siblings; no call-site changes needed.
- Redaction improvements (short passphrases, hostnames) are NOT in
  scope — noted in index; shape-based redaction is a documented
  tradeoff.

## Current state
insights.py:1030-1034:
```python
        entry = {"decision": "retired", "at": _now_iso()}
        if args.superseded_by:
            entry["superseded_by"] = args.superseded_by
        data["decisions"][cluster] = entry
```
insights.py:1060-1063 (`_reconcile_retired`):
```python
    for cid in retired:
        if decisions[cid].get("superseded_by") not in existing:
            del decisions[cid]
            cleared += 1
```
insights.py:124-125:
```python
def _ensure_root() -> None:
    insights_root().mkdir(parents=True, exist_ok=True)
```

## Tasks
Wave 1
- [ ] `_reconcile_retired`: skip entries lacking `superseded_by`
      (condition: `"superseded_by" in dec and dec["superseded_by"] not
      in existing`); update its docstring — Reuse: insights.py:1051-1067
      — 0.5 SP
- [ ] `_ensure_root`: 0700 root (mkdir mode + explicit chmod) — Reuse:
      insights.py:124-125 — 0.5 SP
- [ ] `chmod 755 plugins/wise/scripts/insights.py` — Reuse: file mode —
      0.5 SP

Wave 2
- [ ] Tests (tmp XDG_DATA_HOME): retire-without-supersede survives a
      reconcile; retire-with-supersede clears when the superseding skill
      dir is removed; insights root is 0700 after any command — New:
      plugins/wise/tests/test_insights_decisions.py — 1 SP

Total: 2 SP (rounded)

## Testing
New: plugins/wise/tests/test_insights_decisions.py (pattern: PLAN-001's
importlib + tmp XDG_DATA_HOME approach; insights.py resolves its root
via `_wise_data_root` at insights.py:96 which honours XDG_DATA_HOME).

## Validation
- `python3 -m py_compile plugins/wise/scripts/insights.py` → exits 0
- `python3 -m pytest plugins/wise/tests/test_insights_decisions.py -q` → all pass
- `ls -l plugins/wise/scripts/insights.py` → `-rwxr-xr-x`
- `XDG_DATA_HOME=$(mktemp -d) python3 plugins/wise/scripts/insights.py status >/dev/null 2>&1; stat -f '%Lp' "$XDG_DATA_HOME/wise/insights"` → `700` (macOS stat; use `stat -c '%a'` on Linux)
- Version bump: plugins/wise/.claude-plugin/plugin.json patch bump

## Stop conditions
- If docs/wise/insights.md explicitly documents that un-superseded
  retires are MEANT to resurface, STOP — the fix inverts to removing
  the dead decision write instead; report first.
- If the SessionEnd hook or any skill invokes insights.py in a way that
  breaks under 0700 (different user context), STOP and report.
- Redaction logic: do not touch.
