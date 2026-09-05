import { after, describe, test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  answerFromDecisions,
  clipReportData,
  clipReportText,
  createChildTracker,
  progressLine,
  resolveContextKey,
  startStaleWatch,
  staleNudgeText,
  writeCheckpoint,
} from "../src/channel.ts";
import type { ChannelTimers } from "../src/channel.ts";
import { findRunDir, listRunDirs } from "../src/daemon.ts";
import type { DaemonRuntime } from "../src/daemon.ts";
import { daemonPaths } from "../src/daemon.ts";
import { createExecutor } from "../src/executor.ts";
import type { Executor, ExecutorOptions } from "../src/executor.ts";
import { initState, readEvents, readState, utcNow } from "../src/ledger.ts";
import { domainError } from "../src/rpc.ts";
import type { CallContext } from "../src/rpc.ts";
import type { RawEvent, RunSummary, State } from "../src/types.ts";
import { fakeAdapter, pause, schemaAnswer } from "./fixtures/executor/fake.ts";
import { heldStarter } from "./fixtures/executor/held.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFS = join(HERE, "..", "..", "workflows");
const EXEC_FIXTURES = join(HERE, "fixtures", "executor");
const VERSION = "9.9.9-channel-test";

const roots: string[] = [];
const executors = new Set<Executor>();

type Root = { root: string; env: Record<string, string>; cwd: string; rt: DaemonRuntime };

function mkRoot(): Root {
  const root = mkdtempSync(join(tmpdir(), "wc-"));
  roots.push(root);
  const env = { XDG_DATA_HOME: root, HOME: root, PATH: process.env.PATH ?? "" };
  const paths = daemonPaths({ env });
  const cwd = join(root, "proj");
  mkdirSync(cwd);
  const rt: DaemonRuntime = {
    paths,
    version: VERSION,
    pid: process.pid,
    started_at: utcNow(),
    log: () => {},
    listRunDirs: () => listRunDirs(paths.runsRoot),
    findRunDir: (id) => findRunDir(paths.runsRoot, id),
    requireRunDir: (id) => {
      const dir = findRunDir(paths.runsRoot, id);
      if (!dir) throw domainError("RUN_NOT_FOUND", `no such run: ${id}`, { run_id: id });
      return dir;
    },
  };
  return { root, env, cwd, rt };
}

const ctx: CallContext = {
  notify: () => {},
  signal: new AbortController().signal,
  connectionId: 0,
};

function make(r: Root, opts: ExecutorOptions = {}): Executor {
  const exec = createExecutor(r.rt, {
    env: r.env,
    roots: { userRoot: EXEC_FIXTURES, bundledRoot: DEFS },
    configPath: join(r.root, "missing.json"),
    adapters: { claude: fakeAdapter("claude", (req) => schemaAnswer(req)) },
    ...opts,
  });
  executors.add(exec);
  return exec;
}

async function until(pred: () => boolean, what: string, timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (pred()) return;
    await pause(10);
  }
  assert.fail(`timed out waiting for ${what}`);
}

/** Manual clock and timer queue: `advance(ms)` fires what is due, in order. */
function fakeTimers(): ChannelTimers & { advance: (ms: number) => void; pending: () => number } {
  let now = 1_000_000;
  type Entry = { at: number; fn: () => void; cancelled: boolean };
  const queue: Entry[] = [];
  return {
    now: () => now,
    setTimeout: (fn, ms) => {
      const e: Entry = { at: now + ms, fn, cancelled: false };
      queue.push(e);
      return e;
    },
    clearTimeout: (h) => {
      (h as Entry).cancelled = true;
    },
    advance(ms) {
      const target = now + ms;
      for (;;) {
        const due = queue
          .filter((e) => !e.cancelled && e.at <= target)
          .toSorted((a, b) => a.at - b.at)[0];
        if (!due) break;
        queue.splice(queue.indexOf(due), 1);
        now = due.at;
        due.fn();
      }
      now = target;
    },
    pending: () => queue.filter((e) => !e.cancelled).length,
  };
}

function raw(parsed: unknown): RawEvent {
  return { ts: "2026-09-05T00:00:00Z", harness: "claude", line: JSON.stringify(parsed), parsed };
}

function assistant(tool: string | undefined, usage?: Record<string, number>): RawEvent {
  const content =
    tool === undefined ? [{ type: "text", text: "..." }] : [{ type: "tool_use", name: tool }];
  return raw({ type: "assistant", message: { content, ...(usage ? { usage } : {}) } });
}

describe("channel", () => {
  after(() => {
    for (const e of executors) e.stop();
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  // ---- pure units ---------------------------------------------------------------------------------------

  test("tracker: turns, last tool, tokens; emits on tool change or every throttle window, never more", () => {
    const t = fakeTimers();
    const tracker = createChildTracker({ step: "work", throttleMs: 30_000, now: t.now });
    const emitted: string[] = [];
    const feed = (e: RawEvent, afterMs: number): void => {
      t.advance(afterMs);
      const p = tracker.ingest(e);
      if (p) emitted.push(progressLine(p));
    };
    feed(raw({ type: "system", subtype: "init", tools: [] }), 0);
    feed(assistant("Read", { input_tokens: 1000, output_tokens: 20 }), 1000);
    feed(assistant("Read", { input_tokens: 1000, output_tokens: 20 }), 1000);
    feed(assistant("Edit", { input_tokens: 1000, output_tokens: 20 }), 1000);
    feed(assistant(undefined, { output_tokens: 100 }), 1000);
    feed(assistant("Edit"), 5000);
    feed(assistant("Edit"), 30_000);
    feed(assistant("Edit"), 29_000);
    feed(raw({ type: "result", usage: { input_tokens: 40_000, output_tokens: 2000 } }), 1000);
    assert.deepEqual(emitted, [
      "turn 1, tool Read, 1k tokens",
      "turn 3, tool Edit, 3k tokens",
      "turn 6, tool Edit, 3k tokens",
      "turn 7, tool Edit, 42k tokens",
    ]);
    const snap = tracker.snapshot();
    assert.equal(snap.turn, 7);
    assert.equal(snap.tokens, 42_000);
    assert.equal(snap.tool, "Edit");
    assert.equal(snap.reports, 0);
    assert.equal(snap.last_activity, new Date(t.now()).toISOString());
    tracker.report();
    assert.equal(tracker.snapshot().reports, 1);
    assert.equal(progressLine(tracker.snapshot()), "turn 7, tool Edit, 42k tokens, 1 report");
    assert.equal(
      progressLine({ step: "s", turn: 0, tokens: 12, last_activity: "", reports: 0 }),
      "turn 0, 12 tokens",
    );
  });

  test("stale watch: nudge after one idle window, kill after another; activity resets; asks pause it", () => {
    const t = fakeTimers();
    let lastActivity = t.now();
    let paused = false;
    const nudges: number[] = [];
    let killed = 0;
    const watch = startStaleWatch({
      staleMs: 10_000,
      timers: t,
      lastActivityMs: () => lastActivity,
      paused: () => paused,
      nudge: (idle) => {
        nudges.push(idle);
        return true;
      },
      kill: () => {
        killed += 1;
      },
    });
    t.advance(6000);
    lastActivity = t.now();
    t.advance(4000); // fires at 10 s: idle only 4 s, re-armed for the remaining 6 s
    assert.deepEqual(nudges, []);
    t.advance(6000);
    assert.deepEqual(nudges, [10_000]);
    assert.equal(killed, 0);
    t.advance(5000);
    lastActivity = t.now(); // the nudge worked: activity resumes
    t.advance(5000);
    assert.equal(killed, 0);
    paused = true; // a question to the human is open: idle is not stale
    t.advance(30_000);
    assert.equal(nudges.length, 1);
    assert.equal(killed, 0);
    paused = false;
    t.advance(10_000);
    assert.equal(nudges.length, 2, "a new idle stretch gets its own nudge");
    t.advance(10_000);
    assert.equal(killed, 1);
    assert.equal(t.pending(), 0, "nothing armed after the kill");
    watch.stop();

    // No stdin: the first window kills.
    let killedNow = 0;
    startStaleWatch({
      staleMs: 5000,
      timers: t,
      lastActivityMs: () => t.now() - 5000,
      nudge: () => false,
      kill: () => {
        killedNow += 1;
      },
    });
    t.advance(5000);
    assert.equal(killedNow, 1);
    assert.match(
      staleNudgeText(600_000),
      /^You have been idle for 10 minutes\. Finish with your structured result now\.$/,
    );
    assert.match(staleNudgeText(20_000), /1 minute\./);
  });

  test("report clipping, decisions matching, context lookup, checkpoint write", () => {
    assert.equal(clipReportText("  a\n b  c ".repeat(1)), "a");
    assert.equal(clipReportText("x".repeat(300)).length, 200);
    assert.deepEqual(clipReportData({ a: 1 }), { a: 1 });
    assert.equal(clipReportData({ a: "y".repeat(1100) }), undefined);
    assert.equal(clipReportData("not an object"), undefined);

    const decisions = { "Base branch?": "main", scope: "backend only" };
    assert.equal(answerFromDecisions("base branch?", ["main", "dev"], decisions), "main");
    assert.equal(
      answerFromDecisions("What scope do we keep?", undefined, decisions),
      "backend only",
    );
    assert.equal(answerFromDecisions("Pick one", ["x", "backend only"], decisions), "backend only");
    assert.equal(answerFromDecisions("Pick one", ["x", "y"], decisions), "x");
    assert.equal(answerFromDecisions("Anything?", undefined, decisions), undefined);
    assert.equal(answerFromDecisions("Anything?", [], undefined), undefined);

    const r = mkRoot();
    const runDir = join(r.root, "run");
    const state: State = initState({
      runDir,
      runId: "01RUNCHANNEL",
      workflow: { name: "w", version: 2, dir: r.root },
      stepIds: ["a", "b"],
      cwd: r.cwd,
      profile: "medium",
    });
    state.context = { guidance: "g", ticket: [{ ref: "T-1", body: "body" }], links: ["u"] };
    state.inputs = { topic: "t" };
    state.outputs = { plan: "the plan", topic: "output wins" };
    state.steps.a = { status: "completed", attempts: 1, outputs: { plan: "the plan", n: 2 } };
    assert.equal(resolveContextKey(state, "guidance"), "g");
    assert.equal(resolveContextKey(state, "ticket.0.body"), "body");
    assert.deepEqual(resolveContextKey(state, "links"), ["u"]);
    assert.equal(resolveContextKey(state, "plan"), "the plan");
    assert.equal(resolveContextKey(state, "topic"), "output wins");
    assert.deepEqual(resolveContextKey(state, "a"), { plan: "the plan", n: 2 });
    assert.equal(resolveContextKey(state, "a.n"), 2);
    assert.equal(resolveContextKey(state, "b"), null);
    assert.equal(resolveContextKey(state, "zzz"), null);
    assert.equal(resolveContextKey(state, ""), null);

    const path = writeCheckpoint(runDir, "a", { partial: true });
    assert.equal(path, join(runDir, "checkpoints", "a.json"));
    assert.deepEqual(JSON.parse(readFileSync(path, "utf8")), { partial: true });
    writeCheckpoint(runDir, "a", null);
    assert.equal(readFileSync(path, "utf8"), "null\n");
    assert.equal(existsSync(path + ".tmp"), false);
  });

  // ---- through the executor ------------------------------------------------------------------------------

  test("stale policy on a Claude child: warn + nudge, then kill; the step fails as `stale` with its cursor", async () => {
    const r = mkRoot();
    const t = fakeTimers();
    const { starter, held } = heldStarter();
    const exec = make(r, { startAgent: starter, channel: { timers: t, staleAfterSecs: 60 } });
    const { run_id } = await exec.handlers.run(
      { workflow: "single-agent", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    await until(() => held.length === 1, "child start");
    const runDir = r.rt.requireRunDir(run_id);
    t.advance(60_000);
    assert.deepEqual(held[0]?.nudges, [staleNudgeText(60_000)]);
    assert.deepEqual(held[0]?.kills, []);
    let events = readEvents(runDir);
    assert.ok(
      events.some((e) => e.type === "warn" && e.message === "answer: idle for 1 min, nudged"),
    );
    t.advance(60_000);
    assert.deepEqual(held[0]?.kills, ["SIGTERM"]);
    await until(() => readState(runDir).status === "failed", "the run to fail");
    const state = readState(runDir);
    assert.equal(state.steps.answer?.status, "failed");
    assert.equal(state.steps.answer?.error, "stale");
    assert.equal(state.steps.answer?.verdict, "failed: stale (no activity, killed)");
    assert.equal(state.steps.answer?.cursor, "sess-held", "E8: cursor kept for a unit resume");
    assert.equal(state.error, "answer: stale");
    events = readEvents(runDir);
    assert.ok(events.some((e) => e.type === "warn" && e.message === "answer: stale, killed"));
    assert.equal(exec.liveRuns().length, 0);
  });

  test("stale policy without stdin (no nudge): the first idle window kills and asks for a cursor resume", async () => {
    const r = mkRoot();
    const t = fakeTimers();
    const { starter, held } = heldStarter();
    const noStdin: typeof starter = (h, req, onEvent) => {
      const handle = starter(h, req, onEvent);
      delete handle.nudge;
      return handle;
    };
    const exec = make(r, { startAgent: noStdin, channel: { timers: t, staleAfterSecs: 30 } });
    const { run_id } = await exec.handlers.run(
      { workflow: "single-agent", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    await until(() => held.length === 1, "child start");
    const runDir = r.rt.requireRunDir(run_id);
    t.advance(29_000);
    assert.deepEqual(held[0]?.kills, []);
    t.advance(1000);
    assert.deepEqual(held[0]?.kills, ["SIGTERM"]);
    await until(() => readState(runDir).status === "failed", "the run to fail");
    const events = readEvents(runDir);
    assert.ok(
      events.some(
        (e) => e.type === "warn" && e.message === "answer: stale, killed; resume from cursor",
      ),
    );
    assert.equal(
      events.filter((e) => e.type === "warn" && /nudged/.test(e.message ?? "")).length,
      0,
    );
  });

  test("step.progress from the stream is throttled per step; status shows children; nudge delivers", async () => {
    const r = mkRoot();
    const t = fakeTimers();
    const { starter, held } = heldStarter();
    const exec = make(r, {
      startAgent: starter,
      channel: { timers: t, progressThrottleMs: 30_000 },
    });
    const { run_id } = await exec.handlers.run(
      { workflow: "single-agent", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    await until(() => held.length === 1, "child start");
    const runDir = r.rt.requireRunDir(run_id);
    const child = held[0]!;
    child.push({ type: "system", subtype: "init", tools: [] });
    child.push({ type: "assistant", message: { content: [{ type: "tool_use", name: "Read" }] } });
    child.push({ type: "assistant", message: { content: [{ type: "tool_use", name: "Read" }] } });
    t.advance(10_000);
    child.push({ type: "assistant", message: { content: [{ type: "text", text: "thinking" }] } });
    t.advance(25_000);
    child.push({ type: "assistant", message: { content: [{ type: "text", text: "still" }] } });
    const progress = readEvents(runDir).filter((e) => e.type === "step.progress");
    assert.deepEqual(
      progress.map((e) => e.message),
      ["turn 1, tool Read, 0 tokens", "turn 4, tool Read, 0 tokens"],
    );
    assert.ok(progress.every((e) => e.step === "answer" && e.kind === undefined));

    const one = (await exec.handlers.status({ run_id }, ctx)) as RunSummary;
    assert.deepEqual(one.children, [
      {
        step: "answer",
        turn: 4,
        tool: "Read",
        tokens: 0,
        last_activity: new Date(t.now()).toISOString(),
        reports: 0,
      },
    ]);
    const all = (await exec.handlers.status({}, ctx)) as RunSummary[];
    assert.equal(all[0]?.children?.length, 1);

    assert.deepEqual(
      await exec.handlers.nudge({ run_id, step: "answer", message: "wrap up" }, ctx),
      { delivered: true },
    );
    assert.deepEqual(child.nudges, ["wrap up"]);
    assert.deepEqual(await exec.handlers.nudge({ run_id, step: "nope", message: "x" }, ctx), {
      delivered: false,
    });

    child.finish(schemaAnswer(child.req));
    await until(() => readState(runDir).status === "completed", "run completion");
    const done = (await exec.handlers.status({ run_id }, ctx)) as RunSummary;
    assert.equal(done.children, undefined, "no live children once the step ended");
    assert.deepEqual(
      await exec.handlers.nudge({ run_id, step: "answer", message: "too late" }, ctx),
      { delivered: false },
    );
    assert.throws(
      () => exec.handlers.nudge({ run_id: "01NOPE", step: "answer", message: "x" }, ctx),
      /no such run/,
    );
  });

  test("channel injection can be switched off; default engine root points at engine.sh", async () => {
    const r = mkRoot();
    const { starter, held } = heldStarter();
    const exec = make(r, { startAgent: starter, channel: { inject: false } });
    const { run_id } = await exec.handlers.run(
      { workflow: "single-agent", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    await until(() => held.length === 1, "child start");
    assert.equal(held[0]?.req.mcp_config, undefined);
    held[0]?.finish(schemaAnswer(held[0].req));
    await until(() => readState(r.rt.requireRunDir(run_id)).status === "completed", "done");

    const r2 = mkRoot();
    const second = heldStarter();
    const exec2 = make(r2, { startAgent: second.starter });
    await exec2.handlers.run(
      { workflow: "single-agent", cwd: r2.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    await until(() => second.held.length === 1, "child start");
    const servers = second.held[0]?.req.mcp_config?.mcpServers as
      | Record<string, { args: string[]; env: Record<string, string> }>
      | undefined;
    const server = servers?.["wise-engine"];
    assert.ok(server);
    assert.ok(existsSync(server.args[0] ?? ""), `engine.sh exists at ${server.args[0]}`);
    assert.equal(server.args[1], "unit-mcp");
    assert.equal(server.env.WISE_ENGINE_SOCKET, r2.rt.paths.socketPath);
    assert.equal(server.env.WISE_DATA_ROOT, r2.rt.paths.dataRoot);
    second.held[0]?.finish(schemaAnswer(second.held[0].req));
    await until(() => exec2.liveRuns().length === 0, "second run completion");
  });
});
