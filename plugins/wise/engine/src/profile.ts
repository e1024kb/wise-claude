// Session token-budget profile store plus the path / session helpers it needs.
// Port of workflows.py: wise_data_root, plugin_data_root, _cwd_slug, _cwd_session_dir,
// _synthetic_session_id, _current_session_id, _profile_dir, _profile_safe_sid,
// cmd_profile_set, cmd_profile_get, cmd_session_path, cmd_session_label.
// Every filesystem location is derived from the injectable `ProfileEnv`.

import {
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";

import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { PROFILE_LEVELS } from "./types.ts";
import type { ProfileLevel } from "./types.ts";

export const PROFILE_DEFAULT: ProfileLevel = "medium";
/** Sibling profile files older than this are pruned on every `profileSet`. */
export const PROFILE_GC_SECONDS = 30 * 24 * 3600;

import { cwdOf, envOf, homeOf, wiseDataRoot, wiseRunsRootForCwd, cwdSlug } from "./paths.ts";
import type { RootOpts } from "./paths.ts";
export { pluginDataRoot, wiseDataRoot } from "./paths.ts";
export type { Env } from "./paths.ts";

/** Injectable environment. Every field falls back to the live process. */
export type ProfileEnv = RootOpts & {
  /** Epoch seconds; injectable for GC tests. */
  now?: () => number;
};

/** `<data root>/runs/<cwd-slug>`; shared shape with the ledger. */
export function runsRootForCwd(opts: ProfileEnv = {}): string {
  return wiseRunsRootForCwd(opts);
}

// ---- session ids ---------------------------------------------------------------------

export function cwdSessionDir(opts: ProfileEnv = {}): string {
  return join(homeOf(opts), ".claude", "projects", cwdSlug(cwdOf(opts)));
}

/** Stable per-workspace id for harnesses that expose no session concept. */
export function syntheticSessionId(opts: ProfileEnv = {}): string {
  const slug = cwdSlug(cwdOf(opts)).replace(/^-+|-+$/g, "");
  return "local-" + (slug || "workspace");
}

function isFile(path: string): boolean {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}
function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/**
 * Harness session id: `CLAUDE_CODE_SESSION_ID`, then `WISE_SESSION_ID`, then the newest
 * `.jsonl` transcript in the cwd's Claude project dir, then the synthetic id. Never empty.
 */
export function currentSessionId(opts: ProfileEnv = {}): string {
  const env = envOf(opts);
  for (const name of ["CLAUDE_CODE_SESSION_ID", "WISE_SESSION_ID"] as const) {
    const sid = (env[name] ?? "").trim();
    if (sid) return sid;
  }
  const dir = cwdSessionDir(opts);
  if (isDir(dir)) {
    let newest: { mtime: number; stem: string } | undefined;
    for (const entry of readdirSync(dir)) {
      if (!entry.endsWith(".jsonl")) continue;
      const full = join(dir, entry);
      let mtime: number;
      try {
        const st = statSync(full);
        if (!st.isFile()) continue;
        mtime = st.mtimeMs;
      } catch {
        continue;
      }
      if (newest === undefined || mtime > newest.mtime) {
        newest = { mtime, stem: entry.slice(0, -".jsonl".length) };
      }
    }
    if (newest) return newest.stem;
  }
  return syntheticSessionId(opts);
}

/** Transcript path for a session id in this cwd, or null when there is none. */
export function sessionPath(sessionId: string, opts: ProfileEnv = {}): string | null {
  const path = join(cwdSessionDir(opts), `${sessionId}.jsonl`);
  return isFile(path) ? path : null;
}

/** `<run-id>_<first seven hyphen tokens of the workflow name>` for `/resume`'s picker. */
export function sessionLabel(runId: string, workflowName: string): string {
  const tokens = workflowName.split("-").filter(Boolean).slice(0, 7);
  return `${runId}_${tokens.join("-") || "workflow"}`;
}

// ---- profile store ---------------------------------------------------------------------

/** `<data root>/profile`; one file per session id holding one level word. */
export function profileDir(opts: ProfileEnv = {}): string {
  return join(wiseDataRoot(opts), "profile");
}

// Session ids become file names; a hostile env value must not traverse out of the store.
const SESSION_ID_FILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

/** The current session id when it is safe to use as a file name, else null. */
export function profileSafeSid(opts: ProfileEnv = {}): string | null {
  const sid = currentSessionId(opts);
  if (!sid || !SESSION_ID_FILE_RE.test(sid) || sid === "." || sid === "..") return null;
  return sid;
}

export function isProfileLevel(value: string): value is ProfileLevel {
  return (PROFILE_LEVELS as readonly string[]).includes(value);
}

export type ProfileSetResult =
  | { ok: true; level: ProfileLevel; session: string; path: string }
  | { ok: false; error: "profile-level" | "profile-no-session"; message: string };

/**
 * Store the level for the current session (atomic tmp + rename), then prune sibling files
 * older than `PROFILE_GC_SECONDS`. Filesystem errors from the write propagate; GC errors
 * are swallowed.
 */
export function profileSet(rawLevel: string, opts: ProfileEnv = {}): ProfileSetResult {
  const level = rawLevel.trim().toLowerCase();
  if (!isProfileLevel(level)) {
    return { ok: false, error: "profile-level", message: `INVALID:profile-level:${level}` };
  }
  const sid = profileSafeSid(opts);
  if (!sid) {
    return { ok: false, error: "profile-no-session", message: "INVALID:profile-no-session" };
  }
  const dir = profileDir(opts);
  mkdirSync(dir, { recursive: true });
  const target = join(dir, sid);
  const tmp = join(dir, `.tmp-profile-${randomBytes(6).toString("hex")}`);
  try {
    writeFileSync(tmp, level + "\n", "utf8");
    renameSync(tmp, target);
  } catch (err) {
    try {
      unlinkSync(tmp);
    } catch {
      // best effort
    }
    throw err;
  }
  const nowSec = opts.now ? opts.now() : Date.now() / 1000;
  const cutoffMs = (nowSec - PROFILE_GC_SECONDS) * 1000;
  let entries: string[] = [];
  try {
    entries = readdirSync(dir);
  } catch {
    entries = [];
  }
  for (const name of entries) {
    if (name === sid) continue;
    const full = join(dir, name);
    try {
      const st = statSync(full);
      if (st.isFile() && st.mtimeMs < cutoffMs) unlinkSync(full);
    } catch {
      // best effort
    }
  }
  return { ok: true, level, session: sid, path: target };
}

/** The stored level for the current session; any failure degrades to `PROFILE_DEFAULT`. */
export function profileGet(opts: ProfileEnv = {}): ProfileLevel {
  const sid = profileSafeSid(opts);
  if (sid) {
    try {
      const level = readFileSync(join(profileDir(opts), sid), "utf8")
        .trim()
        .toLowerCase();
      if (isProfileLevel(level)) return level;
    } catch {
      // missing or unreadable store
    }
  }
  return PROFILE_DEFAULT;
}
