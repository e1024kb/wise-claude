// Port of plugins/wise/tests/test_profile.py (test names kept) plus session-id helpers.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import {
  PROFILE_GC_SECONDS,
  currentSessionId,
  cwdSessionDir,
  pluginDataRoot,
  profileDir,
  profileGet,
  profileSet,
  runsRootForCwd,
  sessionLabel,
  sessionPath,
  syntheticSessionId,
  wiseDataRoot,
} from "../src/profile.ts";
import type { ProfileEnv } from "../src/profile.ts";

/** Isolated env: XDG_DATA_HOME, HOME and cwd all under one temp dir (Python `wise_env`). */
function wiseEnv(
  sid: string | null = "sess-profile-1",
  extra: Record<string, string> = {},
): ProfileEnv & { tmp: string } {
  const tmp = mkdtempSync(join(tmpdir(), "wise-profile-"));
  const home = join(tmp, "home");
  mkdirSync(home);
  const env: Record<string, string> = { XDG_DATA_HOME: tmp, ...extra };
  if (sid !== null) env.CLAUDE_CODE_SESSION_ID = sid;
  return { env, home, cwd: tmp, tmp };
}

function isFile(path: string): boolean {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

test("test_profile_get_missing_store_defaults_medium", () => {
  const e = wiseEnv();
  assert.equal(profileGet(e), "medium");
});

test("test_profile_set_get_roundtrip", () => {
  const e = wiseEnv();
  const res = profileSet("low", e);
  assert.ok(res.ok);
  assert.equal(res.level, "low");
  assert.equal(res.session, "sess-profile-1");
  assert.equal(profileGet(e), "low");
});

test("test_profile_set_normalises_case_and_whitespace", () => {
  const e = wiseEnv();
  assert.ok(profileSet("  MAX ", e).ok);
  assert.equal(profileGet(e), "max");
});

test("test_profile_set_invalid_level_exits_2", () => {
  const e = wiseEnv();
  const res = profileSet("turbo", e);
  assert.ok(!res.ok);
  assert.equal(res.error, "profile-level");
  assert.match(res.message, /INVALID:profile-level:turbo/);
});

test("test_profile_get_garbage_content_defaults_medium", () => {
  const e = wiseEnv();
  const dir = profileDir(e);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "sess-profile-1"), "weird value\n");
  assert.equal(profileGet(e), "medium");
});

test("test_profile_store_honours_xdg_data_home", () => {
  const e = wiseEnv();
  assert.ok(profileSet("low", e).ok);
  const stored = join(e.tmp, "wise", "profile", "sess-profile-1");
  assert.ok(isFile(stored));
  assert.equal(readFileSync(stored, "utf8").trim(), "low");
});

test("test_profile_set_failed_replace_leaves_no_temp", () => {
  // A non-empty directory at the target makes rename() fail, like Python's patched os.replace.
  const e = wiseEnv();
  const dir = profileDir(e);
  mkdirSync(join(dir, "sess-profile-1"), { recursive: true });
  writeFileSync(join(dir, "sess-profile-1", "block"), "x");
  assert.throws(() => profileSet("low", e));
  const leftovers = readdirSync(dir).filter((n) => n.startsWith(".tmp-profile-"));
  assert.deepEqual(leftovers, []);
});

test("test_profile_set_prunes_stale_siblings", () => {
  const e = wiseEnv("sess-current");
  const dir = profileDir(e);
  mkdirSync(dir, { recursive: true });
  const stale = join(dir, "sess-dead");
  writeFileSync(stale, "low\n");
  const old = Date.now() / 1000 - (PROFILE_GC_SECONDS + 3600);
  utimesSync(stale, old, old);
  const fresh = join(dir, "sess-alive");
  writeFileSync(fresh, "max\n");

  assert.ok(profileSet("medium", e).ok);
  assert.ok(!existsSync(stale));
  assert.ok(existsSync(fresh));
});

test("test_profile_set_rejects_traversal_session_id", () => {
  for (const evil of ["../../../../tmp/pwned", "/etc/foo", "..", "a/b"]) {
    const e = wiseEnv(evil);
    const res = profileSet("low", e);
    assert.ok(!res.ok);
    assert.equal(res.error, "profile-no-session");
    assert.match(res.message, /INVALID:profile-no-session/);
    const wouldBe = resolve(profileDir(e), evil);
    assert.ok(!isFile(wouldBe));
  }
});

test("test_profile_get_traversal_session_id_defaults_medium", () => {
  const e = wiseEnv("../outside");
  assert.equal(profileGet(e), "medium");
});

test("test_profile_get_non_utf8_store_defaults_medium", () => {
  const e = wiseEnv();
  const dir = profileDir(e);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "sess-profile-1"), Buffer.from([0xff, 0xfe, 0x00, 0x67, 0x61, 0x72]));
  assert.equal(profileGet(e), "medium");
});

test("test_wise_session_id_used_when_claude_var_absent", () => {
  const e = wiseEnv(null, { WISE_SESSION_ID: "wise-sess-9" });
  const res = profileSet("max", e);
  assert.ok(res.ok);
  assert.equal(res.session, "wise-sess-9");
  assert.equal(profileGet(e), "max");
});

// ---- session helpers (ported from _current_session_id / cmd_session_* / data roots) --------

test("currentSessionId falls back to the newest transcript, then the synthetic id", () => {
  const e = wiseEnv(null);
  assert.equal(currentSessionId(e), syntheticSessionId(e));
  assert.match(syntheticSessionId(e), /^local-[^/]+$/);
  const dir = cwdSessionDir(e);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "older.jsonl"), "{}");
  const past = Date.now() / 1000 - 600;
  utimesSync(join(dir, "older.jsonl"), past, past);
  writeFileSync(join(dir, "newer.jsonl"), "{}");
  writeFileSync(join(dir, "ignored.txt"), "");
  assert.equal(currentSessionId(e), "newer");
  assert.equal(sessionPath("newer", e), join(dir, "newer.jsonl"));
  assert.equal(sessionPath("missing", e), null);
});

test("sessionLabel keeps the first seven name tokens", () => {
  assert.equal(sessionLabel("01ABC", "ticket-plan"), "01ABC_ticket-plan");
  assert.equal(sessionLabel("01ABC", "a-b-c-d-e-f-g-h-i"), "01ABC_a-b-c-d-e-f-g");
  assert.equal(sessionLabel("01ABC", "---"), "01ABC_workflow");
});

test("data roots honour XDG_DATA_HOME, CLAUDE_PLUGIN_DATA and WISE_DATA_DIR", () => {
  const home = "/h";
  assert.equal(wiseDataRoot({ env: {}, home }), join(home, ".local", "share", "wise"));
  assert.equal(wiseDataRoot({ env: { XDG_DATA_HOME: "/x" }, home }), "/x/wise");
  assert.equal(pluginDataRoot({ env: { XDG_DATA_HOME: "/x" }, home }), "/x/wise");
  assert.equal(pluginDataRoot({ env: { WISE_DATA_DIR: "/w", XDG_DATA_HOME: "/x" }, home }), "/w");
  assert.equal(
    pluginDataRoot({ env: { CLAUDE_PLUGIN_DATA: "/c", WISE_DATA_DIR: "/w" }, home }),
    "/c",
  );
  assert.equal(
    runsRootForCwd({ env: { XDG_DATA_HOME: "/x" }, home, cwd: "/a/b" }),
    "/x/wise/runs/-a-b",
  );
});
