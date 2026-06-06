#!/usr/bin/env python3
# wise plugin — session-insights subsystem (self-improving skills)
#
# Mines Claude Code's own session transcripts (the *.jsonl files Claude
# Code already writes under ~/.claude/projects/<cwd-slug>/) for RECURRING
# task patterns, and — once a pattern recurs across enough distinct
# sessions — surfaces it as a candidate the `/wise-insights-mine` skill
# can draft into a reusable ~/.claude/skills/<name>/SKILL.md.
#
# Two callers, two cost profiles:
#   • the SessionEnd hook calls `ingest <transcript>` — O(one file), no
#     LLM, always exits 0, must never block session teardown.
#   • the `/wise-insights-mine` skill calls `mine` — self-heals (re-ingests
#     changed/unseen transcripts in scope), clusters, gates, and prints
#     gated candidates for the skill to act on.
#
# STDLIB ONLY. This file must import nothing outside the standard library
# so the SessionEnd hook works on a fresh install, BEFORE `/wise-init` /
# bootstrap-deps.sh has installed yaml/ulid. (That is also why we do NOT
# `import workflows` unconditionally — workflows.py hard-imports yaml/ulid
# at module load. We import its `wise_data_root` when it's available and
# fall back to an exact mirror otherwise; see below.)
#
# Subcommands:
#   ingest <transcript_path> [--session-id <id>]
#                       → upsert ONE session into the ledger. Idempotent on
#                         (session_id, transcript_mtime). Always exit 0.
#   mine [--here] [--since <Nd>] [--min-count <N>] [--json]
#                       → self-heal + cluster + gate; rewrite candidate
#                         store; print gated candidates.
#   list-candidates [--json]            → dump candidate store
#   show-candidate <cluster_id> [--json] → full evidence for one candidate
#   mark <cluster_id> <promoted|dismissed> [--skill-name <name>] [--skill-path <p>]
#                       → record a decision so the pattern is never
#                         re-proposed (suppression list).
#   purge [--yes]       → delete all insights state (privacy escape hatch)
#   data-root           → print wise_data_root()/insights (shell-side seam)
#
# The refine pass (the `/wise-insights-refine` "garden" command):
#   list-skills [--json] [--skills-dir <p>]
#                       → enumerate ~/.claude/skills/, classify managed (carry
#                         the wise-insights marker) vs external, reconcile vs
#                         decisions.json. Read-only.
#   overlap [--json] [--min-jaccard <f>] [--include-external] [--skills-dir <p>]
#                       → deterministic pairwise Jaccard edges between skills
#                         (frontmatter/boilerplate stripped, corpus-DF filtered)
#                         for the skill to confirm + merge. Read-only.
#   retire <skill_path> [--superseded-by <name>] [--json]
#                       → THE destructive op: refuse unless the skill carries the
#                         marker, back up (verified) then remove the dir, and flag
#                         its source cluster `retired`. Non-deterministic exception.
#
# DETERMINISM CONTRACT: same ledger + same flags ⇒ byte-identical `mine` /
# `overlap` output. Sorted iteration everywhere, sha1 cluster ids, no randomness,
# no network. `ingest` / `retire` / `purge` are the documented non-deterministic
# (filesystem/timestamp) exceptions. NOTE: cluster_id is derived from a pattern's recurring-vocabulary
# fingerprint (see `_cluster_key`). Changing the normalizer / fingerprint
# logic changes ids and therefore invalidates existing decisions.json
# suppression — treat normalization changes as breaking.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent


# ---- persistent per-user data root -----------------------------------------
#
# The wise invariant: all persistent state routes through `wise_data_root()`
# in workflows.py (single source of truth, never hard-code paths). But that
# module hard-imports yaml/ulid, which may be absent when the SessionEnd hook
# runs on a fresh install. So: use the canonical helper when importable, and
# fall back to an EXACT mirror otherwise. Post-bootstrap (the common case) the
# canonical function is used, so a future relocation in workflows.py still
# propagates here. Keep the fallback identical to workflows.wise_data_root().

def _wise_data_root() -> Path:
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from workflows import wise_data_root  # type: ignore

        return wise_data_root()
    except Exception:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else HOME / ".local" / "share"
        return base / "wise"


def insights_root() -> Path:
    return _wise_data_root() / "insights"


def ledger_path() -> Path:
    return insights_root() / "ledger.jsonl"


def candidates_path() -> Path:
    return insights_root() / "candidates.json"


def decisions_path() -> Path:
    return insights_root() / "decisions.json"


def _ensure_root() -> None:
    insights_root().mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- transcript discovery --------------------------------------------------

def projects_root() -> Path:
    return HOME / ".claude" / "projects"


def _cwd_slug(p: Path) -> str:
    return str(p.resolve()).replace("/", "-")


def discover_transcripts(here: bool, since_days: int | None) -> list[Path]:
    """In-scope *.jsonl transcripts.

    `here` restricts to the current cwd's project dir (Claude Code names it
    with the same slug shape as `_cwd_slug`). `since_days` drops files whose
    mtime is older than the cutoff (bounds the first full backfill)."""
    root = projects_root()
    if not root.is_dir():
        return []
    if here:
        dirs = [root / _cwd_slug(Path.cwd())]
    else:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    cutoff = None
    if since_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp()
    out: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            try:
                if cutoff is not None and f.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            out.append(f)
    return sorted(out)


# ---- transcript parsing ----------------------------------------------------

# Markers of injected / non-typed content that appears as type=="user" but is
# not a human request: slash-command echoes, harness reminders, task plumbing,
# and the auto-generated compaction continuation preamble.
_CMD_MARKERS = ("<command-name>", "<command-message>", "<local-command",
                "<command-args>", "<command-stdout>", "<task-notification>",
                "<system-reminder>", "<tool-use-id>", "<task-id>",
                "<bash-input>", "<bash-stdout>", "<bash-stderr>")

# Prefixes that mark an automated / headless invocation (a persona/system
# prompt the user's own tooling sent to Claude, not an interactive request).
_AUTO_PREFIXES = (
    "this session is being continued",
    "caveat:",
    "you are ",            # agent/persona system prompts
    "your task is",
    "your job is",
)

# Genuine interactive requests sit in this length band. Below it is chatter;
# far above it is almost always a pasted system/persona prompt from headless
# automation (memory extractors, summarizers, consolidation agents).
MIN_PROMPT_LEN = 4
MAX_PROMPT_LEN = 1200


def iter_session_events(path: Path):
    """Yield one dict per JSONL line, skipping unparseable lines. Streams —
    never loads the whole (up to 14 MB) file into memory."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        return


def _is_genuine_prompt(ev: dict) -> str | None:
    """Return the user's prompt text, or None if this event is not a genuine
    typed prompt (meta/sidechain/tool-result/slash-command echoes excluded)."""
    if ev.get("type") != "user":
        return None
    if ev.get("isMeta") or ev.get("isSidechain"):
        return None
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, str):  # tool_result turns are lists — skip
        return None
    text = content.strip()
    if not (MIN_PROMPT_LEN <= len(text) <= MAX_PROMPT_LEN):
        return None
    if any(m in text for m in _CMD_MARKERS):
        return None
    low = text.lower()
    if low.startswith(_AUTO_PREFIXES):
        return None
    return text


def _tool_names(ev: dict):
    if ev.get("type") != "assistant":
        return
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                yield name


def _runlength_dedup(seq: list[str], cap: int = 12) -> list[str]:
    out: list[str] = []
    for x in seq:
        if not out or out[-1] != x:
            out.append(x)
        if len(out) >= cap:
            break
    return out


# ---- redaction (privacy) ---------------------------------------------------
#
# Cleaned prompt text IS persisted in the ledger, so scrub obvious secrets
# and identifiers BEFORE they are stored. Tool INPUTS are never stored at all
# (only tool names), which is where most secrets/file-bodies live.

_RE_URL = re.compile(r"https?://\S+")
_RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_RE_PATH = re.compile(r"(?:/Users/|/home/|/var/|/etc/|/opt/|/private/)[\w./@+-]+")
_RE_SECRET = re.compile(r"\b(?:sk-|ghp_|gho_|github_pat_|xox[baprs]-|AKIA|ASIA)[\w-]+")
_RE_LONGTOK = re.compile(r"\b[A-Za-z0-9_-]{28,}\b")  # jwt/ulid/sha/api-key shaped

# Applied in order — broad/specific secret shapes before the catch-all long-token
# rule. Add new patterns here; `redact()` needs no change.
_REDACTIONS = [
    (_RE_URL, "<url>"),
    (_RE_SECRET, "<secret>"),
    (_RE_EMAIL, "<email>"),
    (_RE_PATH, "<path>"),
    (_RE_LONGTOK, "<token>"),
]

REDACT_LIMIT = 240  # max stored chars per prompt (truncated with an ellipsis)


def redact(text: str, limit: int = REDACT_LIMIT) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


# ---- session record extraction --------------------------------------------

MAX_PROMPTS_PER_SESSION = 50


def extract_session_record(path: Path, session_id: str | None) -> dict | None:
    """Stream a transcript into a compact session record, or None if it holds
    no genuine user prompts (nothing to learn from)."""
    prompts: list[str] = []
    raw_tools: list[str] = []
    cwd = ""
    branch = ""
    first_ts = ""
    last_ts = ""
    for ev in iter_session_events(path):
        if not isinstance(ev, dict):
            continue
        c = ev.get("cwd")
        if isinstance(c, str) and c:
            cwd = c
        b = ev.get("gitBranch")
        if isinstance(b, str) and b:
            branch = b
        ts = ev.get("timestamp")
        if isinstance(ts, str) and ts:
            if not first_ts:
                first_ts = ts
            last_ts = ts
        p = _is_genuine_prompt(ev)
        if p is not None and len(prompts) < MAX_PROMPTS_PER_SESSION:
            prompts.append(redact(p))
        raw_tools.extend(_tool_names(ev))
    if not prompts:
        return None
    sid = session_id or path.stem
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return {
        "session_id": sid,
        "transcript_path": str(path),
        "cwd": cwd,
        "git_branch": branch,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "transcript_mtime": mtime,
        "ingested_at": _now_iso(),
        "prompts": prompts,
        "tool_sig": _runlength_dedup(raw_tools),
        "prompt_count": len(prompts),
    }


# ---- ledger I/O (JSONL, last-wins on read) ---------------------------------

def read_ledger() -> dict[str, dict]:
    """session_id → record. Append-with-last-wins: later lines for the same
    session replace earlier ones."""
    out: dict[str, dict] = {}
    p = ledger_path()
    if not p.is_file():
        return out
    for ev in iter_session_events(p):
        sid = ev.get("session_id") if isinstance(ev, dict) else None
        if isinstance(sid, str) and sid:
            out[sid] = ev
    return out


def append_ledger(record: dict) -> None:
    _ensure_root()
    with ledger_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def compact_ledger(records: dict[str, dict]) -> None:
    """Rewrite the ledger to one line per session (drops superseded lines).
    Called opportunistically by `mine` after self-heal."""
    _ensure_root()
    tmp = ledger_path().with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for sid in sorted(records):
            fh.write(json.dumps(records[sid], ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(ledger_path())


# ---- decisions + candidates I/O --------------------------------------------

def read_decisions() -> dict:
    p = decisions_path()
    if not p.is_file():
        return {"decisions": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("decisions"), dict):
            return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {"decisions": {}}


def write_decisions(data: dict) -> None:
    _ensure_root()
    decisions_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_candidates(payload: dict) -> None:
    _ensure_root()
    candidates_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def read_candidates() -> dict:
    p = candidates_path()
    if not p.is_file():
        return {"candidates": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {"candidates": []}


# ---- prompt normalization + clustering -------------------------------------

_STOPWORDS = {
    "the", "and", "for", "you", "your", "with", "this", "that", "have", "has",
    "are", "was", "but", "not", "can", "will", "would", "should", "could",
    "please", "lets", "let", "now", "then", "from", "into", "out", "off",
    "all", "any", "our", "use", "using", "via", "per", "about", "make",
    "made", "want", "need", "like", "claude", "code", "file", "files", "run",
    "also", "just", "get", "got", "set", "new", "one", "two", "some", "more",
    "here", "there", "when", "what", "which", "how", "why", "who", "its",
    "their", "them", "they", "i", "we", "to", "of", "in", "on", "is", "it",
    "a", "an", "be", "do", "did", "so", "if", "as", "at", "or", "my", "me",
}

_RE_PLACEHOLDER = re.compile(r"<\w+>")
_RE_BACKTICK = re.compile(r"`[^`]*`")
_RE_WORD = re.compile(r"[a-z]+")


def _stem(tok: str) -> str:
    if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def normalize_tokens(prompt: str) -> set[str]:
    """Recurring-vocabulary fingerprint of one (already-redacted) prompt:
    lowercase, drop code spans / placeholders / digits, tokenize to words,
    drop stopwords and short tokens, light singular stemming."""
    s = prompt.lower()
    s = _RE_BACKTICK.sub(" ", s)
    s = _RE_PLACEHOLDER.sub(" ", s)
    toks = set()
    for w in _RE_WORD.findall(s):
        if len(w) < 3 or w in _STOPWORDS:
            continue
        toks.add(_stem(w))
    return toks


# ---- clustering / heuristic thresholds -------------------------------------
MIN_TOKEN_DF = 2            # a token must recur across >=2 prompts to be "shared"
MIN_TOKEN_SET_SIZE = 2      # prompts with fewer content tokens are one-word chatter
CLUSTER_KEY_TOP_K = 5       # cap a cluster key to its top-K shared tokens
AUTOMATION_MIN_SESSIONS = 4  # byte-identical recurrence over >=N sessions ⇒ machine


def _cluster_key(tokens: frozenset[str], df: dict[str, int],
                 top_k: int = CLUSTER_KEY_TOP_K) -> tuple[str, ...]:
    """Stable canonical key for a prompt's token set: keep only tokens that
    recur across the corpus (df >= MIN_TOKEN_DF — drops one-off noise), rank by
    df desc then alphabetically, cap to top_k. Two phrasings of the same intent
    collapse to the same key; the key is what cluster_id hashes."""
    shared = [t for t in tokens if df.get(t, 0) >= MIN_TOKEN_DF]
    if not shared:
        shared = sorted(tokens)  # all-hapax prompt: fall back to its own tokens
    shared.sort(key=lambda t: (-df.get(t, 0), t))
    return tuple(sorted(shared[:top_k]))


def _cluster_id(key: tuple[str, ...]) -> str:
    return hashlib.sha1((" ".join(key)).encode("utf-8")).hexdigest()[:12]


def cluster_records(records: list[dict]) -> list[dict]:
    """Group prompts across sessions by stable cluster key. Returns one dict
    per cluster with distinct-session counts and evidence, sorted by
    session_count desc then cluster_id."""
    # Pass 1 — corpus document frequency of tokens (one count per prompt).
    df: dict[str, int] = {}
    items: list[tuple[str, str, frozenset[str]]] = []  # (session_id, prompt, tokens)
    for rec in records:
        sid = rec.get("session_id", "")
        for prompt in rec.get("prompts", []):
            toks = normalize_tokens(prompt)
            if not toks:
                continue
            fz = frozenset(toks)
            items.append((sid, prompt, fz))
            for t in fz:
                df[t] = df.get(t, 0) + 1

    # Pass 2 — bucket by stable cluster key.
    sig_by_session = {rec.get("session_id", ""): rec.get("tool_sig", []) for rec in records}
    buckets: dict[str, dict] = {}
    for sid, prompt, fz in items:
        if len(fz) < MIN_TOKEN_SET_SIZE:
            continue  # one-word chatter ("done", "ok", "going") — low skill value
        key = _cluster_key(fz, df)
        if not key:
            continue
        cid = _cluster_id(key)
        b = buckets.get(cid)
        if b is None:
            b = {
                "cluster_id": cid,
                "key": list(key),
                "sessions": set(),
                "prompt_counter": {},
            }
            buckets[cid] = b
        b["sessions"].add(sid)
        b["prompt_counter"][prompt] = b["prompt_counter"].get(prompt, 0) + 1

    clusters: list[dict] = []
    for cid, b in buckets.items():
        sessions = sorted(b["sessions"])
        ranked_prompts = sorted(b["prompt_counter"].items(), key=lambda kv: (-kv[1], kv[0]))
        example_prompts = [p for p, _ in ranked_prompts[:3]]
        label = ranked_prompts[0][0] if ranked_prompts else " ".join(b["key"])
        tool_sigs = []
        seen_sig = set()
        for sid in sessions:
            sig = tuple(sig_by_session.get(sid, []))
            if sig and sig not in seen_sig:
                seen_sig.add(sig)
                tool_sigs.append(list(sig))
            if len(tool_sigs) >= 3:
                break
        # distinct_phrasings vs session_count: a ratio near 1 means varied
        # human wording (likely a genuine recurring task); a ratio near 0
        # (many sessions, one identical string) flags machine-generated /
        # headless prompts the reviewer will usually want to dismiss.
        clusters.append({
            "cluster_id": cid,
            "key": b["key"],
            "label": label,
            "session_count": len(sessions),
            "distinct_phrasings": len(b["prompt_counter"]),
            "sessions": sessions,
            "example_prompts": example_prompts,
            "tool_sigs": tool_sigs,
        })
    clusters.sort(key=lambda c: (-c["session_count"], c["cluster_id"]))
    return clusters


# ---- window filtering ------------------------------------------------------

def _parse_ts(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def filter_records(records: list[dict], here: bool, since_days: int | None) -> list[dict]:
    out = records
    if here:
        cwd = str(Path.cwd().resolve())
        out = [r for r in out if r.get("cwd") == cwd]
    if since_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp()
        out = [r for r in out if _parse_ts(r.get("last_ts", "")) >= cutoff]
    return out


# ---- subcommands -----------------------------------------------------------

def cmd_ingest(args) -> int:
    # Hook-safe: any failure must still exit 0 so it never blocks teardown.
    try:
        path = Path(args.transcript_path)
        if not path.is_file():
            return 0
        existing = read_ledger().get(args.session_id or path.stem)
        if existing:
            try:
                if existing.get("transcript_mtime") == path.stat().st_mtime:
                    return 0  # unchanged — idempotent no-op
            except OSError:
                pass
        record = extract_session_record(path, args.session_id)
        if record is None:
            return 0
        append_ledger(record)
    except Exception as exc:  # never propagate out of the hook
        print(f"insights.py ingest: {exc}", file=sys.stderr)
    return 0


def _self_heal(here: bool, since_days: int | None) -> tuple[dict[str, dict], int]:
    """Re-ingest in-scope transcripts that are new or changed since last seen.
    Returns (ledger_by_session, newly_ingested_count)."""
    ledger = read_ledger()
    ingested = 0
    for path in discover_transcripts(here, since_days):
        sid = path.stem
        prev = ledger.get(sid)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if prev and prev.get("transcript_mtime") == mtime:
            continue
        rec = extract_session_record(path, sid)
        if rec is None:
            continue
        ledger[sid] = rec
        ingested += 1
    if ingested:
        compact_ledger(ledger)
    return ledger, ingested


def _is_automated(c: dict) -> bool:
    """A pattern that recurs byte-identically (no phrasing variation) across
    many sessions is almost always machine-generated (headless/automation),
    not a typed human request. General signal — no hardcoded phrases."""
    return (c.get("distinct_phrasings", 1) == 1
            and c["session_count"] >= AUTOMATION_MIN_SESSIONS)


def cmd_mine(args) -> int:
    ledger, ingested = _self_heal(args.here, args.since)
    # Resurrect patterns whose merged (superseding) skill was deleted — keeps
    # "retired" reversible at the pattern level, not just the file level.
    _reconcile_retired(skills_dir())
    records = filter_records(list(ledger.values()), args.here, args.since)
    decisions = read_decisions()["decisions"]
    clusters = cluster_records(records)

    # Enrich each cluster once with its decision status + automation flag, then
    # derive both the persisted candidate store and the gated set the skill acts
    # on from that single pass — no recomputation.
    gated = []
    candidates = []
    suppressed_auto = 0
    for c in clusters:
        decision = decisions.get(c["cluster_id"])
        status = decision["decision"] if decision else "pending"
        c = {**c, "status": status, "likely_automated": _is_automated(c)}
        if status != "pending" or c["session_count"] >= args.min_count:
            candidates.append({k: v for k, v in c.items() if k != "key"})
        if status != "pending" or c["session_count"] < args.min_count:
            continue
        if c["likely_automated"] and not args.include_automated:
            suppressed_auto += 1
            continue
        gated.append(c)

    payload = {
        "generated_at": _now_iso(),
        "min_count": args.min_count,
        "scope": "here" if args.here else "all",
        "since_days": args.since,
        "sessions_in_scope": len(records),
        "newly_ingested": ingested,
        "suppressed_automated": suppressed_auto,
        "candidates": candidates,
    }
    write_candidates(payload)

    if args.json:
        print(json.dumps({**payload, "gated": gated}, ensure_ascii=False, indent=2))
        return 0

    auto_note = (f" ({suppressed_auto} machine-generated pattern(s) hidden; "
                 f"--include-automated to show)") if suppressed_auto else ""
    if not gated:
        print(f"No recurring patterns reached the threshold "
              f"(min-count={args.min_count}, sessions in scope={len(records)}).{auto_note}")
        if ingested:
            print(f"({ingested} session(s) ingested this run.)")
        return 0
    print(f"Found {len(gated)} candidate pattern(s) "
          f"(>= {args.min_count} sessions, {len(records)} in scope):{auto_note}\n")
    for c in gated:
        print(f"  [{c['cluster_id']}] {c['label']}")
        print(f"      seen in {c['session_count']} sessions "
              f"({c['distinct_phrasings']} phrasings); examples: "
              f"{' | '.join(c['example_prompts'][:2])}")
    return 0


def cmd_list_candidates(args) -> int:
    data = read_candidates()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    cands = data.get("candidates", [])
    if not cands:
        print("No candidates. Run `mine` first.")
        return 0
    for c in cands:
        print(f"  [{c['cluster_id']}] ({c.get('status', 'pending')}) "
              f"x{c['session_count']}  {c['label']}")
    return 0


def cmd_show_candidate(args) -> int:
    for c in read_candidates().get("candidates", []):
        if c["cluster_id"] == args.cluster_id:
            print(json.dumps(c, ensure_ascii=False, indent=2))
            return 0
    print(f"No candidate with id {args.cluster_id}.", file=sys.stderr)
    return 2


def cmd_mark(args) -> int:
    if args.decision not in ("promoted", "dismissed"):
        print("decision must be 'promoted' or 'dismissed'.", file=sys.stderr)
        return 2
    data = read_decisions()
    entry = {"decision": args.decision, "at": _now_iso()}
    if args.skill_name:
        entry["skill_name"] = args.skill_name
    if args.skill_path:
        entry["skill_path"] = args.skill_path
    data["decisions"][args.cluster_id] = entry
    write_decisions(data)
    print(f"Recorded: {args.cluster_id} -> {args.decision}")
    return 0


def cmd_purge(args) -> int:
    if not args.yes:
        print("Refusing to purge without --yes. This deletes the ledger, "
              "candidates, and decisions under:", file=sys.stderr)
        print(f"  {insights_root()}", file=sys.stderr)
        return 2
    import shutil
    root = insights_root()
    if root.exists():
        shutil.rmtree(root)
    print(f"Purged {root}")
    return 0


def cmd_data_root(args) -> int:
    print(insights_root())
    return 0


# ---- skill library: enumerate / overlap / retire (the refine pass) ---------
#
# `list-skills` + `overlap` are read-only and deterministic; `retire` is the one
# destructive op (backup-verify-then-delete, marker-gated). All reuse the same
# stdlib helpers as the mine pass — no yaml: skill name comes from the directory
# (the dir==name convention), the marker is parsed by regex, and overlap is keyed
# on body tokens via `normalize_tokens`.

_RE_MARKER = re.compile(r"<!--\s*wise-insights:\s*(.*?)\s*-->")
_RE_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_RE_HEADING = re.compile(r"^#{1,6}\s.*$", re.MULTILINE)

# Template scaffolding shared by generated skills — excluded from overlap tokens
# so two unrelated learned skills don't match on boilerplate alone.
_SKILL_BOILERPLATE = (
    "when to use", "procedure", "guardrails", "notes",
    "this was learned from", "past sessions", "starting point", "edit freely",
    "review and refine", "session history", "redacted local transcripts",
    "nothing left your machine", "wise-managed", "wise-insights-refine",
    "wise-insights-mine", "use when the user says",
)

OVERLAP_MIN_JACCARD = 0.6   # document-sized token sets; below this over-merges


def skills_dir(override: str | None = None) -> Path:
    """User-global skills dir. `--skills-dir` / env WISE_SKILLS_DIR override it
    (the testability seam, mirroring how XDG_DATA_HOME redirects the data root)."""
    if override:
        return Path(override).expanduser()
    env = os.environ.get("WISE_SKILLS_DIR")
    return Path(env).expanduser() if env else HOME / ".claude" / "skills"


def parse_marker(body: str) -> dict | None:
    """Parse the `<!-- wise-insights: k=v … -->` marker into a dict, or None if
    absent. A dict without `source` means the marker is present but malformed."""
    m = _RE_MARKER.search(body)
    if not m:
        return None
    fields: dict[str, str] = {}
    for tok in m.group(1).split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    return fields


def _extract_description(body: str) -> str:
    """Best-effort `description:` for DISPLAY ONLY — tolerates `>-`/`|` block
    scalars and never raises (a parse miss just yields "")."""
    fm = _RE_FRONTMATTER.search(body)
    if not fm:
        return ""
    inner = fm.group(0).splitlines()[1:-1]
    out: list[str] = []
    capturing = False
    for ln in inner:
        if not capturing:
            mm = re.match(r"description:\s*(.*)$", ln)
            if mm:
                first = mm.group(1).strip()
                if first and first not in (">-", ">", "|", "|-"):
                    out.append(first)
                capturing = True
            continue
        if re.match(r"^[A-Za-z][\w-]*:\s", ln) or ln.strip() == "---":
            break
        out.append(ln.strip())
    return " ".join(out).strip()


def _skill_tokens(body: str) -> set:
    """Discriminating token set for a skill: drop frontmatter, HTML comments
    (incl. the marker), markdown headings, and shared template boilerplate, then
    reuse `normalize_tokens`."""
    text = _RE_FRONTMATTER.sub(" ", body)
    text = _RE_HTML_COMMENT.sub(" ", text)
    text = _RE_HEADING.sub(" ", text)
    low = text.lower()
    for phrase in _SKILL_BOILERPLATE:
        low = low.replace(phrase, " ")
    return normalize_tokens(low)


def read_skill(skill_dir: Path) -> dict | None:
    """Read one skill dir into a record, or None if it has no SKILL.md."""
    sk = skill_dir / "SKILL.md"
    if not sk.is_file():
        return None
    try:
        body = sk.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    marker = parse_marker(body)
    return {
        "name": skill_dir.name,  # dir == name convention
        "path": str(sk),
        "dir": str(skill_dir),
        "managed": marker is not None and "source" in marker,
        "marker_malformed": marker is not None and "source" not in marker,
        "marker": marker or {},
        "description": _extract_description(body),
        "_tokens": _skill_tokens(body),
    }


def _enumerate_skills(sdir: Path) -> list[dict]:
    skills = []
    if sdir.is_dir():
        for d in sorted(sdir.iterdir()):
            if d.is_dir():
                s = read_skill(d)
                if s is not None:
                    skills.append(s)
    return skills


def _public_skill(s: dict) -> dict:
    return {k: v for k, v in s.items() if not k.startswith("_")}


def cmd_list_skills(args) -> int:
    sdir = skills_dir(args.skills_dir)
    skills = _enumerate_skills(sdir)
    names = {s["name"] for s in skills}
    paths = {s["path"] for s in skills}
    decisions = read_decisions()["decisions"]
    stale = []
    for cid, dec in sorted(decisions.items()):
        d = dec.get("decision")
        if d == "promoted" and dec.get("skill_path") and dec["skill_path"] not in paths:
            stale.append({"cluster_id": cid, "decision": d,
                          "skill_path": dec["skill_path"], "remineable": True})
        elif d == "retired" and dec.get("superseded_by") and dec["superseded_by"] not in names:
            stale.append({"cluster_id": cid, "decision": d,
                          "superseded_by": dec["superseded_by"], "remineable": True})
    out = {"skills_dir": str(sdir),
           "skills": [_public_skill(s) for s in skills],
           "stale_decisions": stale}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    managed = [s for s in skills if s["managed"]]
    print(f"{len(skills)} skill(s) under {sdir} ({len(managed)} wise-managed):\n")
    for s in skills:
        tag = "managed" if s["managed"] else ("malformed-marker" if s["marker_malformed"] else "external")
        print(f"  [{tag}] {s['name']}")
    if stale:
        print(f"\n{len(stale)} stale decision(s) — superseding skill gone, "
              f"pattern can resurface on next mine.")
    return 0


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _former_sibling(a: dict, b: dict) -> bool:
    """True if a was merged FROM b (so they must not be re-offered for merge)."""
    mf = a["marker"].get("merged_from", "")
    return b["name"] in mf.split(",") if mf else False


def cmd_overlap(args) -> int:
    sdir = skills_dir(args.skills_dir)
    skills = _enumerate_skills(sdir)
    n = len(skills)
    # Corpus-DF filter: drop tokens present in EVERY skill (non-discriminating).
    df: dict[str, int] = {}
    for s in skills:
        for t in s["_tokens"]:
            df[t] = df.get(t, 0) + 1
    for s in skills:
        s["_ftokens"] = ({t for t in s["_tokens"] if df.get(t, 0) < n}
                         if n > 1 else set(s["_tokens"]))

    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = skills[i], skills[j]
            both_external = not a["managed"] and not b["managed"]
            mixed = a["managed"] != b["managed"]
            if (both_external or mixed) and not args.include_external:
                continue
            if _former_sibling(a, b) or _former_sibling(b, a):
                continue
            jac = _jaccard(a["_ftokens"], b["_ftokens"])
            if jac < args.min_jaccard:
                continue
            edges.append({
                "a": a["name"], "b": b["name"],
                "a_path": a["path"], "b_path": b["path"],
                "jaccard": round(jac, 3),
                "retire_eligible": a["managed"] and b["managed"],
                "suggestion_only": both_external or mixed,
                "shared_tokens": sorted(a["_ftokens"] & b["_ftokens"])[:12],
            })
    edges.sort(key=lambda e: (-e["jaccard"], e["a"], e["b"]))
    out = {"skills_dir": str(sdir), "skills": n,
           "min_jaccard": args.min_jaccard, "edges": edges}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if not edges:
        print(f"No overlapping skills (>= {args.min_jaccard} Jaccard) among "
              f"{n} skill(s) in {sdir}.")
        return 0
    print(f"{len(edges)} overlapping pair(s) (>= {args.min_jaccard}):\n")
    for e in edges:
        kind = "" if e["retire_eligible"] else "  (suggestion only)"
        print(f"  {e['a']} ~ {e['b']}  jaccard={e['jaccard']}{kind}")
        print(f"      shared: {', '.join(e['shared_tokens'][:8])}")
    return 0


def _now_micro() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def cmd_retire(args) -> int:
    import shutil
    p = Path(args.skill_path).expanduser()
    # Accept either the skill dir or its SKILL.md; resolve by name so an
    # already-deleted dir still resolves correctly (is_dir() would mislead).
    skill_dir = p.parent if p.name == "SKILL.md" else p
    sk = skill_dir / "SKILL.md"
    if not sk.is_file():
        print(f"already retired (no SKILL.md at {skill_dir})")
        return 0
    body = sk.read_text(encoding="utf-8", errors="replace")
    marker = parse_marker(body)
    if not marker or "source" not in marker:
        print(f"refusing to retire {skill_dir.name}: no wise-insights marker — "
              f"hand-written skills are never auto-deleted.", file=sys.stderr)
        return 2
    # Backup (verified) BEFORE removing anything.
    backup = insights_root() / "skill-backups" / _now_micro() / skill_dir.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(skill_dir, backup)
    except OSError as exc:
        print(f"backup failed for {skill_dir.name} ({exc}); aborting retire.",
              file=sys.stderr)
        return 2
    bsk = backup / "SKILL.md"
    if not bsk.is_file() or bsk.stat().st_size == 0:
        print(f"backup verification failed for {skill_dir.name}; aborting retire.",
              file=sys.stderr)
        return 2
    shutil.rmtree(skill_dir)
    # Bookkeeping: a source with a cluster id becomes `retired` (covered), NOT
    # `dismissed` (rejected). Refine-merged sources (merged_from, no cluster)
    # touch no decision.
    cluster = marker.get("cluster")
    if cluster:
        data = read_decisions()
        entry = {"decision": "retired", "at": _now_iso()}
        if args.superseded_by:
            entry["superseded_by"] = args.superseded_by
        data["decisions"][cluster] = entry
        write_decisions(data)
    result = {"retired": skill_dir.name, "backup": str(backup), "cluster": cluster}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Retired {skill_dir.name} → backup {backup}")
    return 0


def _reconcile_retired(sdir: Path) -> int:
    """Clear `retired` decisions whose superseding skill no longer exists, so the
    now-uncovered pattern can resurface in `mine`. Returns count cleared."""
    existing = {d.name for d in sdir.iterdir()
                if d.is_dir() and (d / "SKILL.md").is_file()} if sdir.is_dir() else set()
    data = read_decisions()
    decisions = data["decisions"]
    cleared = 0
    for cid in list(decisions):
        dec = decisions[cid]
        if dec.get("decision") == "retired" and dec.get("superseded_by") not in existing:
            del decisions[cid]
            cleared += 1
    if cleared:
        write_decisions(data)
    return cleared


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="insights.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest")
    p.add_argument("transcript_path")
    p.add_argument("--session-id", dest="session_id", default=None)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("mine")
    p.add_argument("--here", action="store_true")
    p.add_argument("--since", type=_since_days, default=None)
    p.add_argument("--min-count", dest="min_count", type=int, default=3)
    p.add_argument("--include-automated", dest="include_automated", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_mine)

    p = sub.add_parser("list-candidates")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list_candidates)

    p = sub.add_parser("show-candidate")
    p.add_argument("cluster_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show_candidate)

    p = sub.add_parser("mark")
    p.add_argument("cluster_id")
    p.add_argument("decision")
    p.add_argument("--skill-name", dest="skill_name", default=None)
    p.add_argument("--skill-path", dest="skill_path", default=None)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("purge")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("data-root")
    p.set_defaults(func=cmd_data_root)

    p = sub.add_parser("list-skills")
    p.add_argument("--skills-dir", dest="skills_dir", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list_skills)

    p = sub.add_parser("overlap")
    p.add_argument("--skills-dir", dest="skills_dir", default=None)
    p.add_argument("--min-jaccard", dest="min_jaccard", type=float,
                   default=OVERLAP_MIN_JACCARD)
    p.add_argument("--include-external", dest="include_external", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_overlap)

    p = sub.add_parser("retire")
    p.add_argument("skill_path")
    p.add_argument("--superseded-by", dest="superseded_by", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_retire)

    args = ap.parse_args(argv)
    return args.func(args)


def _since_days(raw: str) -> int:
    """Accept '30d', '30', '2w' → days."""
    m = re.fullmatch(r"(\d+)\s*([dw]?)", raw.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(f"bad --since value: {raw!r} (try 30d)")
    n = int(m.group(1))
    return n * 7 if m.group(2) == "w" else n


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
