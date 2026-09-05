import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  appendEvent,
  appendRawLog,
  applyWorktreeInclude,
  cwdSlug,
  envPositiveInt,
  findRunsBySession,
  formatSessionRunRow,
  initState,
  listResumableRuns,
  listRuns,
  listUnits,
  logPaths,
  newUlid,
  pluginDataRoot,
  pruneRuns,
  readEvents,
  readState,
  readUnit,
  recordOutput,
  resetRunning,
  sessionIsFresh,
  startRun,
  startStep,
  statePath,
  updateRun,
  updateStep,
  utcNow,
  wiseDataRoot,
  wiseRunsRootForCwd,
  writeLog,
  writeState,
  writeUnit,
  LedgerError,
} from "../src/ledger.ts";
import type { Exec } from "../src/ledger.ts";
import type { State, UnitLedger } from "../src/types.ts";

// ---- fixtures ------------------------------------------------------------------

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "wise-ledger-"));
}

/** Port of conftest `wise_env`: XDG_DATA_HOME + cwd both under one tmp dir. */
function wiseEnv(): { root: string; runsRoot: string; env: Record<string, string> } {
  const root = tmp();
  const env = { XDG_DATA_HOME: root };
  const runsRoot = wiseRunsRootForCwd({ env, cwd: root });
  mkdirSync(runsRoot, { recursive: true });
  return { root, runsRoot, env };
}

const DEMO = { name: "demo", version: 1, dir: "/defs/demo" };

function initDemo(root: string, runId = "run-1", harnessSession: string | null = null): string {
  const runDir = join(root, runId);
  initState({ runDir, runId, workflow: DEMO, stepIds: ["a", "b"], cwd: root, harnessSession });
  return runDir;
}

function writeLooseState(runDir: string, data: Record<string, unknown>): void {
  writeState(runDir, data as unknown as State);
}

/** Port of test_prune_runs `_make_run`. */
function makeRun(runsRoot: string, runId: string, status: string, lastActivityAt: string): string {
  const runDir = join(runsRoot, runId);
  writeLooseState(runDir, { status, last_activity_at: lastActivityAt });
  return runDir;
}

/** Port of test_state_lifecycle `_run_state`. */
function runState(runDir: string, status: string, extra: Record<string, unknown> = {}): void {
  writeLooseState(runDir, {
    run_id: runDir.split("/").at(-1),
    workflow: DEMO,
    status,
    last_activity_at: "2026-01-01T00:00:00Z",
    ...extra,
  });
}

// ---- utilities / roots -----------------------------------------------------------------

test("utcNow has one-second granularity and ends with Z", () => {
  assert.match(utcNow(new Date("2026-05-01T12:34:56.789Z")), /^2026-05-01T12:34:56Z$/);
});

test("envPositiveInt falls back on unset, non-numeric and non-positive", () => {
  assert.equal(envPositiveInt("X", 25, {}), 25);
  assert.equal(envPositiveInt("X", 25, { X: "abc" }), 25);
  assert.equal(envPositiveInt("X", 25, { X: "0" }), 25);
  assert.equal(envPositiveInt("X", 25, { X: "-3" }), 25);
  assert.equal(envPositiveInt("X", 25, { X: "7" }), 7);
});

test("sessionIsFresh: missing or malformed is stale, recent is fresh, future is fresh", () => {
  const now = Date.parse("2026-01-01T00:30:00Z");
  assert.equal(sessionIsFresh(null, 1800, now), false);
  assert.equal(sessionIsFresh("not-a-date", 1800, now), false);
  assert.equal(sessionIsFresh("2026-01-01T00:00:00Z", 1800, now), true);
  assert.equal(sessionIsFresh("2026-01-01T00:00:00Z", 1799, now), false);
  assert.equal(sessionIsFresh("2026-01-01T01:00:00Z", 1800, now), true);
});

test("newUlid is 26 Crockford chars and monotonic within a process", () => {
  const a = newUlid(1000);
  const b = newUlid(1000);
  assert.match(a, /^[0-9A-HJKMNP-TV-Z]{26}$/);
  assert.equal(a.slice(0, 10), b.slice(0, 10));
  assert.ok(b > a);
  const c = newUlid(2000);
  assert.ok(c > b);
});

test("wiseDataRoot honours XDG_DATA_HOME and falls back to ~/.local/share", () => {
  assert.equal(wiseDataRoot({ env: { XDG_DATA_HOME: "/xdg" } }), "/xdg/wise");
  assert.equal(wiseDataRoot({ env: {}, home: "/home/u" }), "/home/u/.local/share/wise");
});

test("pluginDataRoot prefers CLAUDE_PLUGIN_DATA, then WISE_DATA_DIR, then the data root", () => {
  assert.equal(
    pluginDataRoot({ env: { CLAUDE_PLUGIN_DATA: "/cpd", WISE_DATA_DIR: "/wdd" } }),
    "/cpd",
  );
  assert.equal(pluginDataRoot({ env: { WISE_DATA_DIR: "/wdd" } }), "/wdd");
  assert.equal(pluginDataRoot({ env: {}, home: "/home/u" }), "/home/u/.local/share/wise");
});

test("wiseRunsRootForCwd is <data_root>/runs/<cwd-slug>", () => {
  const root = tmp();
  const runsRoot = wiseRunsRootForCwd({ env: { XDG_DATA_HOME: root }, cwd: root });
  assert.equal(runsRoot, join(root, "wise", "runs", cwdSlug(root)));
  assert.ok(!cwdSlug(root).includes("/"));
});

// ---- test_state_lifecycle.py -------------------------------------------------------------

test("test_init_state_writes_stub", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root, "run-1", "sess-1");

  assert.ok(existsSync(statePath(runDir)));
  assert.ok(existsSync(join(runDir, "logs")));

  const state = readState(runDir);
  assert.equal(state.version, 2);
  assert.equal(state.status, "initializing");
  assert.equal(state.run_id, "run-1");
  assert.equal(state.workflow.name, "demo");
  assert.equal(state.harness_session, "sess-1");
  assert.deepEqual(Object.keys(state.steps), ["a", "b"]);
  assert.deepEqual(
    Object.values(state.steps).map((s) => s.status),
    ["pending", "pending"],
  );
  assert.equal(state.profile, "medium");
  assert.deepEqual(state.outputs, {});
  assert.ok(state.last_activity_at.endsWith("Z"));
});

test("initState rejects invalid and duplicate step ids", () => {
  const { root } = wiseEnv();
  assert.throws(
    () => initState({ runDir: join(root, "r"), runId: "r", workflow: DEMO, stepIds: ["Bad"] }),
    (e: unknown) => e instanceof LedgerError && e.code === "INVALID_STEP_ID",
  );
  assert.throws(
    () => initState({ runDir: join(root, "r"), runId: "r", workflow: DEMO, stepIds: ["a", "a"] }),
    (e: unknown) => e instanceof LedgerError && e.code === "DUPLICATE_STEP_ID",
  );
});

test("test_start_run_merges_inputs_and_flips_running", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);
  const before = readState(runDir).last_activity_at;

  // utcNow has one-second granularity; inject a later stamp to prove it is refreshed.
  const state = startRun(
    runDir,
    {
      answers: { "control-mode": "wave-sync" },
      project: { path: "/tmp/proj", name: "proj", kind: "backend" },
      inputs: { ticket: "ABC-1" },
    },
    { now: "2099-01-01T00:00:00Z" },
  );

  assert.equal(state.status, "running");
  assert.equal(state.answers["control-mode"], "wave-sync");
  assert.equal(state.project?.kind, "backend");
  assert.deepEqual(state.outputs, { ticket: "ABC-1" });
  assert.deepEqual(state.inputs, { ticket: "ABC-1" });
  assert.ok(state.last_activity_at > before);
  assert.equal(state.last_activity_at, "2099-01-01T00:00:00Z");
  assert.deepEqual(readState(runDir), state);
});

test("test_update_step_mutates_and_unknown_step_returns_1", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);

  updateStep(runDir, "a", { status: "running" });
  assert.equal(readState(runDir).steps.a?.status, "running");

  assert.throws(
    () => updateStep(runDir, "no-such-step", { status: "running" }),
    (e: unknown) => e instanceof LedgerError && e.code === "NO_SUCH_STEP",
  );
});

test("test_record_output_captures_named_output", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);

  recordOutput(runDir, "pr_url", "https://example/1");
  assert.equal(readState(runDir).outputs.pr_url, "https://example/1");
});

test("test_reset_running_pops_running_fields_and_reverts_to_pending", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);

  const state = readState(runDir);
  Object.assign(state.steps.a as object, {
    status: "running",
    started_at: "2026-01-01T00:00:00Z",
    step_run_id: "sub-run",
  });
  (state.steps.b as { status: string }).status = "completed";
  state.status = "paused";
  writeState(runDir, state);

  const after = resetRunning(runDir);
  const a = after.steps.a;
  assert.equal(a?.status, "pending");
  assert.ok(a && !("started_at" in a));
  assert.ok(a && !("step_run_id" in a));
  assert.equal(after.steps.b?.status, "completed");
  assert.equal(after.status, "running");
});

test("startStep assigns a fresh step ULID on every (re-)run", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);

  const first = startStep(runDir, "a");
  updateStep(runDir, "a", { status: "completed", completed_at: utcNow() });
  const second = startStep(runDir, "a");

  assert.notEqual(first, second);
  const step = readState(runDir).steps.a;
  assert.equal(step?.step_run_id, second);
  assert.equal(step?.status, "running");
  assert.equal(step?.attempts, 2);
  assert.ok(step && !("completed_at" in step));
});

test("updateRun patches top-level fields and refreshes last_activity_at", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);
  const state = updateRun(
    runDir,
    { status: "completed", completed_at: "2026-01-02T00:00:00Z" },
    { now: "2026-01-02T00:00:00Z" },
  );
  assert.equal(state.status, "completed");
  assert.equal(state.completed_at, "2026-01-02T00:00:00Z");
  assert.equal(state.last_activity_at, "2026-01-02T00:00:00Z");
});

test("test_list_resumable_runs_includes_failed_excludes_completed", () => {
  const { runsRoot } = wiseEnv();
  runState(join(runsRoot, "run-failed"), "failed");
  runState(join(runsRoot, "run-completed"), "completed");

  const items = listResumableRuns(runsRoot);
  const ids = new Set(items.map((i) => i.run_id));
  assert.ok(ids.has("run-failed"));
  assert.ok(!ids.has("run-completed"));
  assert.equal(items.find((i) => i.run_id === "run-failed")?.status, "failed");
});

test("listResumableRuns sorts most recently active first", () => {
  const { runsRoot } = wiseEnv();
  runState(join(runsRoot, "old"), "running", { last_activity_at: "2026-01-01T00:00:00Z" });
  runState(join(runsRoot, "new"), "paused", { last_activity_at: "2026-02-01T00:00:00Z" });
  assert.deepEqual(
    listResumableRuns(runsRoot).map((i) => i.run_id),
    ["new", "old"],
  );
});

test("test_find_runs_by_session_reports_fresh_failed_run", () => {
  const { runsRoot } = wiseEnv();
  runState(join(runsRoot, "run-failed"), "failed", {
    harness_session: "sess-1",
    last_activity_at: utcNow(),
  });

  const rows = findRunsBySession(runsRoot, "sess-1", {});
  assert.equal(rows.length, 1);
  const line = formatSessionRunRow(rows[0]!);
  const [runId, workflow, status, , freshness] = line.split("\t");
  assert.equal(runId, "run-failed");
  assert.equal(workflow, "demo");
  assert.equal(status, "failed");
  assert.equal(freshness, "fresh");
});

test("findRunsBySession flags an old run stale and skips other sessions and terminal runs", () => {
  const { runsRoot } = wiseEnv();
  runState(join(runsRoot, "abandoned"), "running", { harness_session: "s" });
  runState(join(runsRoot, "other"), "running", {
    harness_session: "t",
    last_activity_at: utcNow(),
  });
  runState(join(runsRoot, "done"), "completed", {
    harness_session: "s",
    last_activity_at: utcNow(),
  });

  const rows = findRunsBySession(runsRoot, "s", {});
  assert.deepEqual(
    rows.map((r) => [r.run_id, r.fresh]),
    [["abandoned", false]],
  );
});

test("test_profile_outputs_roundtrip_and_render", () => {
  // Render half lives in the render module; this pins the record-output round trip.
  const { root } = wiseEnv();
  const runDir = initDemo(root, "run-p", "sess-p");
  for (const [name, value] of [
    ["run_profile", "low"],
    ["tuning_step_gap_analysis", "sonnet / high"],
    ["team_mode", "solo"],
    ["cap_max_review_cycles", "2"],
  ] as const) {
    recordOutput(runDir, name, value);
  }
  const state = readState(runDir);
  assert.equal(state.outputs.run_profile, "low");
  assert.equal(state.outputs.tuning_step_gap_analysis, "sonnet / high");
  assert.equal(state.outputs.cap_max_review_cycles, "2");
});

test("listRuns lists every run dir with a state file, unreadable ones flagged", () => {
  const { runsRoot } = wiseEnv();
  runState(join(runsRoot, "ok"), "running");
  mkdirSync(join(runsRoot, "broken"));
  writeFileSync(statePath(join(runsRoot, "broken")), "{not json");
  mkdirSync(join(runsRoot, "orphan"));

  const rows = listRuns(runsRoot);
  assert.deepEqual(
    rows.map((r) => [r.run_id, r.status, r.workflow]),
    [
      ["broken", "<unreadable>", ""],
      ["ok", "running", "demo"],
    ],
  );
});

// ---- test_prune_runs.py ----------------------------------------------------------------------

test("test_non_terminal_alone_over_cap_survives", () => {
  const { runsRoot } = wiseEnv();
  const env = { WISE_RUN_HISTORY_CAP: "2" };
  const dirs = [1, 2, 3].map((i) =>
    makeRun(runsRoot, `run-running-${i}`, "running", `2026-01-0${i}T00:00:00Z`),
  );

  const res = pruneRuns(runsRoot, env);
  assert.deepEqual(res.pruned, []);
  assert.ok(dirs.every((d) => existsSync(d)));
});

test("test_mixed_over_cap_deletes_only_terminal", () => {
  const { runsRoot } = wiseEnv();
  const env = { WISE_RUN_HISTORY_CAP: "2" };
  const nonTerm = [1, 2, 3].map((i) =>
    makeRun(runsRoot, `run-running-${i}`, "running", `2026-02-0${i}T00:00:00Z`),
  );
  const term = [1, 2, 3].map((i) =>
    makeRun(runsRoot, `run-completed-${i}`, "completed", `2026-01-0${i}T00:00:00Z`),
  );

  pruneRuns(runsRoot, env);
  assert.ok(nonTerm.every((d) => existsSync(d)));
  assert.ok(term.every((d) => !existsSync(d)));
});

test("test_terminal_budget_partially_filled", () => {
  const { runsRoot } = wiseEnv();
  const env = { WISE_RUN_HISTORY_CAP: "4" };
  const nonTerm = [1, 2].map((i) =>
    makeRun(runsRoot, `run-paused-${i}`, "paused", `2026-03-0${i}T00:00:00Z`),
  );
  // run-completed-3 newest, run-completed-1 oldest.
  const term = [1, 2, 3].map((i) =>
    makeRun(runsRoot, `run-completed-${i}`, "completed", `2026-01-0${i}T00:00:00Z`),
  );

  const res = pruneRuns(runsRoot, env);
  assert.ok(nonTerm.every((d) => existsSync(d)));
  // cap=4, non_term=2 -> only the oldest terminal run goes.
  assert.deepEqual(res.pruned, ["run-completed-1"]);
  assert.ok(!existsSync(term[0]!));
  assert.ok(existsSync(term[1]!));
  assert.ok(existsSync(term[2]!));
});

test("test_under_cap_is_a_noop", () => {
  const { runsRoot } = wiseEnv();
  const env = { WISE_RUN_HISTORY_CAP: "25" };
  const dirs = [1, 2].map((i) =>
    makeRun(runsRoot, `run-completed-${i}`, "completed", `2026-01-0${i}T00:00:00Z`),
  );

  assert.deepEqual(pruneRuns(runsRoot, env), { pruned: [], failed: [] });
  assert.ok(dirs.every((d) => existsSync(d)));
});

test("test_failed_alone_over_cap_survives", () => {
  const { runsRoot } = wiseEnv();
  const env = { WISE_RUN_HISTORY_CAP: "2" };
  const dirs = [1, 2, 3].map((i) =>
    makeRun(runsRoot, `run-failed-${i}`, "failed", `2026-04-0${i}T00:00:00Z`),
  );

  assert.deepEqual(pruneRuns(runsRoot, env).pruned, []);
  assert.ok(dirs.every((d) => existsSync(d)));
});

test("test_mixed_failed_and_completed_keeps_failed_deletes_completed", () => {
  const { runsRoot } = wiseEnv();
  const env = { WISE_RUN_HISTORY_CAP: "2" };
  const failed = [1, 2, 3].map((i) =>
    makeRun(runsRoot, `run-failed-${i}`, "failed", `2026-05-0${i}T00:00:00Z`),
  );
  const completed = [1, 2, 3].map((i) =>
    makeRun(runsRoot, `run-completed-${i}`, "completed", `2026-01-0${i}T00:00:00Z`),
  );

  pruneRuns(runsRoot, env);
  assert.ok(failed.every((d) => existsSync(d)));
  assert.ok(completed.every((d) => !existsSync(d)));
});

test("test_orphan_dir_deleted_before_terminal_run", () => {
  const { runsRoot } = wiseEnv();
  const env = { WISE_RUN_HISTORY_CAP: "1" };
  const completed = makeRun(runsRoot, "run-completed-1", "completed", "2026-01-01T00:00:00Z");
  const orphan = join(runsRoot, "run-orphan");
  mkdirSync(orphan, { recursive: true });

  pruneRuns(runsRoot, env);
  assert.ok(!existsSync(orphan));
  assert.ok(existsSync(completed));
});

test("pruneRuns on a missing runs root is a noop and the default cap is 25", () => {
  const { root, runsRoot } = wiseEnv();
  assert.deepEqual(pruneRuns(join(root, "nope"), {}), { pruned: [], failed: [] });
  for (let i = 0; i < 26; i++) {
    makeRun(
      runsRoot,
      `run-${String(i).padStart(2, "0")}`,
      "completed",
      `2026-01-01T00:00:${String(i).padStart(2, "0")}Z`,
    );
  }
  assert.deepEqual(pruneRuns(runsRoot, {}).pruned, ["run-00"]);
});

// ---- v2: atomic write, events, units, logs -----------------------------------------------------

test("writeState leaves no .tmp behind and the file is complete JSON", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);
  updateRun(runDir, { status: "running" });
  const names = readdirSync(runDir);
  assert.ok(names.includes("state.json"));
  assert.ok(!names.some((n) => n.endsWith(".tmp")));
  assert.equal(JSON.parse(readFileSync(statePath(runDir), "utf8")).status, "running");
});

test("appendEvent numbers seq from the file, so it continues across processes", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);

  const e1 = appendEvent(
    runDir,
    { run_id: "run-1", type: "run.started" },
    { now: "2026-01-01T00:00:00Z" },
  );
  const e2 = appendEvent(runDir, { run_id: "run-1", type: "step.started", step: "a" });
  assert.equal(e1.seq, 1);
  assert.equal(e2.seq, 2);
  assert.equal(e1.ts, "2026-01-01T00:00:00Z");

  // A second "process": only the file carries the counter.
  const e3 = appendEvent(runDir, { run_id: "run-1", type: "step.done", step: "a", verdict: "ok" });
  assert.equal(e3.seq, 3);

  const lines = readFileSync(join(runDir, "events.jsonl"), "utf8").trimEnd().split("\n");
  assert.equal(lines.length, 3);
  assert.deepEqual(
    lines.map((l) => (JSON.parse(l) as { seq: number }).seq),
    [1, 2, 3],
  );
});

test("appendEvent survives a torn trailing line", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);
  appendEvent(runDir, { run_id: "run-1", type: "run.started" });
  writeFileSync(
    join(runDir, "events.jsonl"),
    '{"seq":1,"ts":"x","run_id":"run-1","type":"run.started"}\n{"seq":2,"tr',
    "utf8",
  );
  const e = appendEvent(runDir, { run_id: "run-1", type: "warn", message: "recovered" });
  assert.equal(e.seq, 2);
  assert.deepEqual(
    readEvents(runDir).map((x) => x.seq),
    [1, 2],
  );
});

test("readEvents(after) returns only later events, and [] for a run with none", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);
  assert.deepEqual(readEvents(runDir), []);
  for (const type of ["run.started", "step.started", "step.done", "run.done"] as const) {
    appendEvent(runDir, { run_id: "run-1", type });
  }
  assert.deepEqual(
    readEvents(runDir).map((e) => e.seq),
    [1, 2, 3, 4],
  );
  assert.deepEqual(
    readEvents(runDir, 2).map((e) => [e.seq, e.type]),
    [
      [3, "step.done"],
      [4, "run.done"],
    ],
  );
  assert.deepEqual(readEvents(runDir, 4), []);
});

test("unit ledger round trip, slash in branch name, list", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);
  assert.equal(readUnit(runDir, "feat/ABC-1"), null);

  const ledger: UnitLedger = {
    unit: { ref: "ABC-1", branch: "feat/ABC-1", worktree: "/wt/ABC-1", base: "main" },
    last_phase: "review",
    review: { converged: false, cycles: 2 },
    cleaned: false,
    cursors: { review: { session: "s1" } },
    usage: { input: 10, output: 5, cache_read: 0, cache_write: 0, pool: "subscription" },
  };
  writeUnit(runDir, "feat/ABC-1", ledger);
  writeUnit(runDir, "feat/ABC-2", {
    ...ledger,
    unit: { ...ledger.unit, ref: "ABC-2", branch: "feat/ABC-2" },
  });

  assert.deepEqual(readUnit(runDir, "feat/ABC-1"), ledger);
  const unitFiles = readdirSync(join(runDir, "units"));
  assert.equal(unitFiles.length, 2);
  assert.ok(unitFiles.every((n) => n.endsWith(".json") && !n.includes("/")));
  assert.deepEqual(
    listUnits(runDir).map((u) => u.unit.ref),
    ["ABC-1", "ABC-2"],
  );
});

test("log paths and writeLog / appendRawLog follow logs/<step>.<ulid>.*", () => {
  const { root } = wiseEnv();
  const runDir = initDemo(root);
  const id = newUlid();
  const paths = logPaths(runDir, "a", id);
  assert.equal(paths.log, join(runDir, "logs", `a.${id}.log`));
  assert.equal(paths.raw, join(runDir, "logs", `a.${id}.raw.jsonl`));

  assert.equal(writeLog(runDir, "a", id, "hello\n"), paths.log);
  assert.equal(readFileSync(paths.log, "utf8"), "hello\n");
  appendRawLog(runDir, "a", id, { line: 1 });
  appendRawLog(runDir, "a", id, { line: 2 });
  assert.equal(readFileSync(paths.raw, "utf8"), '{"line":1}\n{"line":2}\n');

  assert.throws(
    () => writeLog(runDir, "../x", id, ""),
    (e: unknown) => e instanceof LedgerError && e.code === "INVALID_STEP_ID",
  );
  assert.throws(
    () => writeLog(runDir, "a", "../x", ""),
    (e: unknown) => e instanceof LedgerError && e.code === "INVALID_STEP_RUN_ID",
  );
});

// ---- test_worktree_include.py -------------------------------------------------------------------

const execWithGhost: Exec = (cmd, args) => {
  const out = execFileSync(cmd, args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  return args.includes("ls-files") ? out + "ghost.txt\0" : out;
};

function git(repo: string, ...args: string[]): void {
  execFileSync("git", ["-C", repo, ...args], { stdio: "pipe" });
}

function initRepo(repo: string): void {
  mkdirSync(repo, { recursive: true });
  git(repo, "init", "-q");
  git(repo, "config", "user.email", "test@example.com");
  git(repo, "config", "user.name", "Test");
  git(repo, "config", "commit.gpgsign", "false");
}

function repoAndWorktree(): { repo: string; worktree: string; base: string } {
  const base = tmp();
  const repo = join(base, "repo");
  const worktree = join(base, "worktree");
  initRepo(repo);
  mkdirSync(worktree);
  return { repo, worktree, base };
}

test("test_untracked_matching_files_are_copied", () => {
  const { repo, worktree } = repoAndWorktree();
  writeFileSync(join(repo, ".worktreeinclude"), ".env\n");
  writeFileSync(join(repo, ".env"), "SECRET=1\n");

  const res = applyWorktreeInclude(repo, worktree);
  assert.equal(res.copied, 1);
  assert.equal(readFileSync(join(worktree, ".env"), "utf8"), "SECRET=1\n");
});

test("test_tracked_files_are_never_copied", () => {
  const { repo, worktree } = repoAndWorktree();
  writeFileSync(join(repo, ".worktreeinclude"), "*.txt\n");
  writeFileSync(join(repo, "tracked.txt"), "tracked content\n");
  git(repo, "add", "tracked.txt");
  git(repo, "commit", "-q", "-m", "add tracked");

  const res = applyWorktreeInclude(repo, worktree);
  assert.equal(res.copied, 0);
  assert.ok(!existsSync(join(worktree, "tracked.txt")));
});

test("test_existing_dest_file_is_overwritten", () => {
  const { repo, worktree } = repoAndWorktree();
  writeFileSync(join(repo, ".worktreeinclude"), "config.local\n");
  writeFileSync(join(repo, "config.local"), "new-value\n");
  writeFileSync(join(worktree, "config.local"), "stale-value\n");

  applyWorktreeInclude(repo, worktree);
  assert.equal(readFileSync(join(worktree, "config.local"), "utf8"), "new-value\n");
});

test("test_escape_entry_is_refused_and_counted_skipped", () => {
  const { repo, worktree, base } = repoAndWorktree();
  const outside = join(base, "outside");
  mkdirSync(outside);
  writeFileSync(join(outside, "secret.txt"), "outside content\n");
  // git lists the symlink's in-tree path; realpath follows it out of the repo.
  symlinkSync(outside, join(repo, "linked"), "dir");
  writeFileSync(join(repo, ".worktreeinclude"), "linked\n");

  const res = applyWorktreeInclude(repo, worktree);
  const err = res.notices.join("\n");
  assert.match(err, /skip out-of-tree path/);
  assert.match(err, /copied 0 \(skipped 1\)/);
  assert.ok(!existsSync(join(worktree, "linked")));
});

test("test_missing_worktreeinclude_prints_notice_and_returns_0", () => {
  const { repo, worktree } = repoAndWorktree();
  const res = applyWorktreeInclude(repo, worktree);
  assert.match(res.notices.join("\n"), /no \.worktreeinclude/);
  assert.equal(res.copied, 0);
});

test("test_git_failure_is_graceful_and_returns_0", () => {
  // Not a git repo at all: `git ls-files` fails, the call still returns normally.
  const base = tmp();
  const repo = join(base, "not-a-repo");
  mkdirSync(repo);
  writeFileSync(join(repo, ".worktreeinclude"), "*.env\n");
  const worktree = join(base, "worktree");
  mkdirSync(worktree);

  const res = applyWorktreeInclude(repo, worktree);
  assert.match(res.notices.join("\n"), /git ls-files failed/);
  assert.equal(res.copied, 0);
});

test("test_ignored_directory_is_copied_via_copytree", () => {
  // `git ls-files --directory` collapses a fully-ignored dir to one `dir/` entry.
  const { repo, worktree } = repoAndWorktree();
  writeFileSync(join(repo, ".worktreeinclude"), "cache/\n");
  mkdirSync(join(repo, "cache"));
  writeFileSync(join(repo, "cache", "nested.bin"), "data\n");

  const res = applyWorktreeInclude(repo, worktree);
  assert.equal(res.copied, 1);
  assert.equal(readFileSync(join(worktree, "cache", "nested.bin"), "utf8"), "data\n");
});

test("test_vanished_listed_path_is_skipped", () => {
  // Inject a phantom entry into the real `git ls-files` output.
  const { repo, worktree } = repoAndWorktree();
  writeFileSync(join(repo, ".worktreeinclude"), "ghost.txt\n");

  const res = applyWorktreeInclude(repo, worktree, { exec: execWithGhost });
  assert.equal(res.copied, 0);
  assert.equal(res.skipped, 1);
  assert.ok(!existsSync(join(worktree, "ghost.txt")));
});
