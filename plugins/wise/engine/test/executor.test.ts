import { after, describe, test } from "node:test";
import assert from "node:assert/strict";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { codexAdapter, grokAdapter } from "../src/adapters/index.ts";
import { connect } from "../src/client.ts";
import type { Client } from "../src/client.ts";
import {
  daemonPaths,
  findRunDir,
  listRunDirs,
  pidAlive,
  readChild,
  startDaemon,
} from "../src/daemon.ts";
import type { Daemon, DaemonRuntime } from "../src/daemon.ts";
import {
  createExecutor,
  DEFAULT_CAPS,
  defaultBackoffMs,
  detectProject,
  executorHandlers,
  loadCaps,
} from "../src/executor.ts";
import type { Executor, ExecutorOptions } from "../src/executor.ts";
import {
  initState,
  newUlid,
  readEvents,
  readState,
  resetRunning,
  startRun,
  startStep,
  updateRun,
  updateStep,
  utcNow,
} from "../src/ledger.ts";
import { RPC_INVALID_PARAMS } from "../src/protocol.ts";
import { domainCode, domainError } from "../src/rpc.ts";
import type { CallContext, RpcError } from "../src/rpc.ts";
import type { Event, RunRes, State } from "../src/types.ts";
import { fakeAdapter, pause, schemaAnswer, usage } from "./fixtures/executor/fake.ts";
import type { FakeAdapter } from "./fixtures/executor/fake.ts";
import { heldStarter } from "./fixtures/executor/held.ts";
import { fakeTimers } from "./fixtures/executor/timers.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
// Bundled root: the real plugin workflows (example-workflow is v2 since M3.1).
const DEFS = join(HERE, "..", "..", "workflows");
const EXEC_FIXTURES = join(HERE, "fixtures", "executor");
const EXAMPLE = join(DEFS, "example-workflow", "workflow.yaml");
const VERSION = "9.9.9-test";

const roots: string[] = [];
const executors = new Set<Executor>();
const daemons = new Set<Daemon>();
const clients = new Set<Client>();

type Root = { root: string; env: Record<string, string>; cwd: string; rt: DaemonRuntime };

function mkRoot(): Root {
  const root = mkdtempSync(join(tmpdir(), "wx-"));
  roots.push(root);
  const env = { XDG_DATA_HOME: root, HOME: root, PATH: process.env.PATH ?? "" };
  const paths = daemonPaths({ env });
  const cwd = join(root, "proj");
  mkdirSync(cwd);
  writeFileSync(join(root, "engine.json"), "{}");
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
    configPath: join(r.root, "engine.json"),
    backoffMs: () => 20,
    ...opts,
  });
  executors.add(exec);
  return exec;
}

function claudeFake(script?: (req: never, n: number) => RunRes): FakeAdapter {
  return fakeAdapter("claude", (req, n) => (script ? script(req as never, n) : schemaAnswer(req)));
}

async function until(pred: () => boolean, what: string, timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (pred()) return;
    await pause(10);
  }
  assert.fail(`timed out waiting for ${what}`);
}

async function untilStatus(r: Root, runId: string, statuses: string[]): Promise<State> {
  const dir = r.rt.requireRunDir(runId);
  await until(() => statuses.includes(readState(dir).status), `status in ${statuses.join("|")}`);
  return readState(dir);
}

/** Run a handler and return its rejection (sync throw or async) as a value. */
async function attempt(fn: () => unknown): Promise<unknown> {
  try {
    return await fn();
  } catch (err) {
    return err;
  }
}

function types(events: Event[]): string[] {
  return events.map((e) => `${e.type}${e.step ? `:${e.step}` : ""}`);
}

const RATE_LIMITED: RunRes = {
  text: "",
  usage: usage(5, 0),
  exit: "rate_limited",
  error: "429 overloaded",
};

function warnsOf(events: Event[]): string[] {
  return events.filter((e) => e.type === "warn").map((e) => e.message ?? "");
}

/** Run a block with `process.env` patched (the real adapters read PATH and vendor vars from it). */
async function withEnv(patch: Record<string, string>, fn: () => Promise<void>): Promise<void> {
  const saved: Record<string, string | undefined> = {};
  for (const [k, v] of Object.entries(patch)) {
    saved[k] = process.env[k];
    process.env[k] = v;
  }
  try {
    await fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  }
}

/** A `<dir>/<name>` shell shim that runs a node script with the CLI's argv. */
function fakeCli(name: string, script: string): string {
  const dir = mkdtempSync(join(tmpdir(), `wise-fake-${name}-`));
  roots.push(dir);
  const file = join(dir, `${name}.cjs`);
  writeFileSync(file, script);
  const bin = join(dir, name);
  writeFileSync(bin, `#!/bin/sh\nexec "${process.execPath}" "${file}" "$@"\n`);
  chmodSync(bin, 0o755);
  return dir;
}

describe("executor", () => {
  after(async () => {
    for (const c of clients) c.close();
    for (const d of daemons) await d.close();
    for (const e of executors) e.stop();
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  // ---- config ---------------------------------------------------------------------------------------

  test("caps: defaults, engine.json, overrides; backoff schedule; project heuristic", () => {
    const r = mkRoot();
    assert.deepEqual(loadCaps(join(r.root, "missing.json")), DEFAULT_CAPS);
    writeFileSync(
      join(r.root, "engine.json"),
      JSON.stringify({ concurrency: { claude: 3, global: 9, grok: 0 } }),
    );
    const caps = loadCaps(join(r.root, "engine.json"), { harness: { codex: 2 } });
    assert.equal(caps.harness.claude, 3);
    assert.equal(caps.global, 9);
    assert.equal(caps.harness.grok, 1, "non-positive values are ignored");
    assert.equal(caps.harness.codex, 2);
    assert.deepEqual(
      [1, 2, 3, 4, 5, 6, 9].map(defaultBackoffMs),
      [1, 2, 4, 8, 16, 30, 30].map((m) => m * 60_000),
    );
    writeFileSync(join(r.root, "go.mod"), "module x");
    assert.deepEqual(detectProject(r.root), {
      path: r.root,
      name: r.root.split("/").at(-1),
      kind: "go",
    });
  });

  // ---- preflight --------------------------------------------------------------------------------------

  test("preflight: by name (user root shadows bundled), by absolute path, not found, invalid", async () => {
    const r = mkRoot();
    const exec = make(r, { adapters: { claude: claudeFake() } });
    const byName = await exec.handlers.preflight({ workflow: "single-agent", cwd: r.cwd }, ctx);
    assert.equal(byName.workflow, "single-agent");
    assert.equal(byName.version, 2);
    assert.deepEqual(
      byName.questions.map((q) => q.id),
      ["profile"],
    );
    const byPath = await exec.handlers.preflight({ workflow: EXAMPLE, cwd: r.cwd }, ctx);
    assert.equal(byPath.workflow, "example-workflow");
    assert.deepEqual(
      byPath.questions.map((q) => q.id),
      ["profile", "tuning.classify", "tuning.summarize", "input.focus"],
    );
    assert.deepEqual(byPath.defaults, {
      profile: "medium",
      "tuning.classify": "default",
      "tuning.summarize": "default",
      "input.focus": "",
    });
    const missing = await attempt(() =>
      exec.handlers.preflight({ workflow: "nope", cwd: r.cwd }, ctx),
    );
    assert.equal(domainCode(missing), "WORKFLOW_NOT_FOUND");
    const gone = await attempt(() =>
      exec.handlers.preflight({ workflow: join(r.root, "gone.yaml"), cwd: r.cwd }, ctx),
    );
    assert.equal(domainCode(gone), "WORKFLOW_NOT_FOUND");
    const invalid = await attempt(() =>
      exec.handlers.preflight({ workflow: "broken", cwd: r.cwd }, ctx),
    );
    assert.equal(domainCode(invalid), "WORKFLOW_INVALID");
    const issues = ((invalid as RpcError).data as { issues: { path: string }[] }).issues;
    assert.ok(issues.some((i) => i.path === "version"));
  });

  test("session profile param: preflight presets the profile default, run uses it when unanswered", async () => {
    const r = mkRoot();
    const exec = make(r, { adapters: { claude: claudeFake() } });
    const pre = await exec.handlers.preflight(
      { workflow: EXAMPLE, cwd: r.cwd, profile: "max" },
      ctx,
    );
    assert.equal(pre.questions.find((q) => q.id === "profile")?.default, "max");
    assert.equal(pre.defaults.profile, "max");
    const bad = await attempt(() =>
      exec.handlers.preflight({ workflow: EXAMPLE, cwd: r.cwd, profile: "turbo" as never }, ctx),
    );
    assert.equal((bad as RpcError).code, RPC_INVALID_PARAMS);

    const { run_id } = await exec.handlers.run(
      { workflow: EXAMPLE, cwd: r.cwd, answers: {}, context: {}, inputs: {}, profile: "max" },
      ctx,
    );
    const runDir = r.rt.requireRunDir(run_id);
    assert.equal(readState(runDir).profile, "max");
    assert.equal(readState(runDir).answers.profile, "max");
    // An explicit answer wins over the session profile.
    const explicit = await exec.handlers.run(
      {
        workflow: EXAMPLE,
        cwd: r.cwd,
        answers: { profile: "low" },
        context: {},
        inputs: {},
        profile: "max",
      },
      ctx,
    );
    assert.equal(readState(r.rt.requireRunDir(explicit.run_id)).profile, "low");
    await exec.handlers.cancel({ run_id }, ctx);
    await exec.handlers.cancel({ run_id: explicit.run_id }, ctx);
  });

  // ---- the example workflow end to end -----------------------------------------------------------------

  test(
    "example workflow: agents, parallel bash, ask gate, approval gate, report",
    { timeout: 30_000 },
    async () => {
      const r = mkRoot();
      const claude = claudeFake();
      const exec = make(r, { adapters: { claude } });
      const { run_id, status } = await exec.handlers.run(
        {
          workflow: EXAMPLE,
          cwd: r.cwd,
          answers: { profile: "medium", "input.focus": "speed" },
          context: { guidance: "be brief" },
          inputs: {},
        },
        ctx,
      );
      assert.equal(status, "running");
      assert.deepEqual(claude.probes, ["subscription"]);
      const runDir = r.rt.requireRunDir(run_id);
      assert.ok(runDir.includes(r.cwd.replaceAll("/", "-")), "run dir under the cwd slug");

      // Wave 1: classify. Wave 2: two agents + three bash steps. Then the ask gate parks the run.
      let state = await untilStatus(r, run_id, ["gated", "failed"]);
      assert.equal(state.status, "gated");
      assert.equal(state.gate?.kind, "ask");
      assert.equal(state.gate?.step, "pick-next");
      assert.deepEqual(
        state.gate?.options?.map((o) => o.value),
        ["tests", "docs", "performance"],
      );
      assert.equal(state.outputs.release_kind, "frontend");
      assert.equal(state.outputs.summary, "summary-value");
      assert.equal(state.inputs.focus, "speed");
      assert.equal(state.project?.name, "proj");
      const summarizePrompt =
        claude.calls.find((c) => c.prompt.includes("summarise"))?.prompt ?? "";
      assert.match(summarizePrompt, /a frontend project called\s+proj/);
      assert.match(summarizePrompt, /Focus: speed/);
      assert.equal(claude.calls[0]?.max_turns, 2);
      assert.equal(claude.calls[0]?.effort, undefined, "haiku has no effort control (resolve)");
      assert.equal(claude.calls[0]?.model, "haiku");
      assert.equal(typeof claude.calls[0]?.step_token, "string");
      assert.equal(claude.calls[0]?.step_token?.length, 32);

      const stale = await attempt(() =>
        exec.handlers.answer({ run_id, gate_id: "01STALE", value: "tests" }, ctx),
      );
      assert.equal(domainCode(stale), "GATE_STALE");

      assert.deepEqual(
        await exec.handlers.answer({ run_id, gate_id: state.gate!.gate_id, value: "tests" }, ctx),
        {
          accepted: true,
        },
      );
      state = await untilStatus(r, run_id, ["gated", "failed", "completed"]);
      assert.equal(state.status, "gated");
      assert.equal(state.gate?.kind, "approval");
      assert.equal(state.gate?.step, "approve-summary");
      assert.match(
        state.gate?.message ?? "",
        /Project: proj \(kind: frontend\) project_emoji-value/,
      );
      assert.match(state.gate?.message ?? "", /Next: tests/);
      assert.equal(state.outputs.next_focus, "tests");
      // bash steps ran for real
      assert.match(String(state.outputs.stamp), /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
      assert.equal(
        state.steps["echo-greeting"]?.verdict,
        "hello from proj (release kind frontend)",
      );
      assert.equal(state.steps["echo-pwd"]?.status, "completed");

      await exec.handlers.answer({ run_id, gate_id: state.gate!.gate_id, value: "approve" }, ctx);
      state = await untilStatus(r, run_id, ["completed", "failed"]);
      assert.equal(state.status, "completed");
      assert.ok(state.completed_at);
      assert.equal(state.gate, undefined);
      for (const [id, s] of Object.entries(state.steps)) {
        assert.equal(s.status, "completed", id);
        assert.ok(s.verdict, `${id} has a verdict`);
        assert.equal(s.attempts, 1, id);
      }
      assert.equal(state.steps.classify?.cursor, "sess-release_kind");
      assert.deepEqual(state.steps.classify?.resolved, {
        harness: "claude",
        model: "haiku",
        effort: "",
        reason: "haiku has no effort control; effort 'low' dropped",
      });

      // E14: usage folded by pool and harness = sum of the three fake results.
      assert.equal(state.usage.subscription.input, 300);
      assert.equal(state.usage.subscription.output, 30);
      assert.equal(state.usage["api-key"].input, 0);
      assert.equal(state.usage.by_harness.claude?.input, 300);

      const report = await exec.handlers.report({ run_id }, ctx);
      assert.deepEqual(report.units, []);
      assert.equal(report.usage.subscription.input, 300);
      assert.deepEqual(
        Object.keys(report.verdicts).toSorted(),
        Object.keys(state.steps).toSorted(),
      );
      assert.equal(report.verdicts["pick-next"], "next_focus=tests");
      assert.equal(report.verdicts["approve-summary"], "approved");

      // Event sequence: compact, ordered, ends with run.done; no output text beyond verdicts.
      const events = readEvents(runDir);
      const seq = types(events);
      assert.equal(seq[0], "run.started");
      assert.equal(seq[1], "step.started:classify");
      assert.equal(seq.at(-1), "run.done");
      assert.ok(seq.indexOf("step.done:classify") < seq.indexOf("step.started:summarize-project"));
      assert.ok(seq.includes("gate.opened:pick-next"));
      assert.ok(
        seq.indexOf("gate.answered:pick-next") < seq.indexOf("gate.opened:approve-summary"),
      );
      assert.equal(events.filter((e) => e.type === "usage").length, 3);
      for (const e of events) {
        assert.ok((e.verdict ?? "").length <= 200);
        assert.ok((e.message ?? "").length <= 200);
      }
      const classifyDone = events.find((e) => e.type === "step.done" && e.step === "classify");
      assert.deepEqual(classifyDone?.outputs, { release_kind: "frontend" });
      assert.equal(classifyDone?.usage?.input, 100);
      const started = events.find((e) => e.type === "step.started" && e.step === "classify");
      assert.equal(started?.harness, "claude");
      assert.equal(started?.model, "haiku");
      assert.equal(started?.effort, undefined);
      // logs: raw stream (none from the fake) and the human extract per agent step
      const logs = readdirSync(join(runDir, "logs"));
      assert.equal(logs.filter((f) => f.endsWith(".log")).length, 3);
      assert.equal(exec.liveRuns().length, 0);
      assert.equal(exec.isBusy(), false);
      assert.equal(existsSync(join(runDir, "daemon.json")), false);
    },
  );

  test("reject flow: approval reject fails the step and the run", { timeout: 30_000 }, async () => {
    const r = mkRoot();
    const exec = make(r, { adapters: { claude: claudeFake() } });
    const { run_id } = await exec.handlers.run(
      { workflow: "approval", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    let state = await untilStatus(r, run_id, ["gated"]);
    assert.equal(state.gate?.message, "Ship prepared?");
    await exec.handlers.answer({ run_id, gate_id: state.gate!.gate_id, value: "reject" }, ctx);
    state = await untilStatus(r, run_id, ["failed", "completed"]);
    assert.equal(state.status, "failed");
    assert.equal(state.steps.approve?.status, "failed");
    assert.equal(state.steps.approve?.verdict, "rejected");
    assert.equal(state.error, "approve: rejected");
    const seq = types(readEvents(r.rt.requireRunDir(run_id)));
    assert.equal(seq.at(-1), "run.failed");
    // a second answer against the closed gate is stale
    const again = await attempt(() =>
      exec.handlers.answer({ run_id, gate_id: state.gate?.gate_id ?? "x", value: "approve" }, ctx),
    );
    assert.equal(domainCode(again), "GATE_STALE");
  });

  test(
    "synchronous control mode auto-approves (def) and answers['control-mode'] overrides the def",
    { timeout: 30_000 },
    async () => {
      const r = mkRoot();
      const exec = make(r, { adapters: { claude: claudeFake() } });
      const a = await exec.handlers.run(
        { workflow: "sync-approval", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
        ctx,
      );
      const sa = await untilStatus(r, a.run_id, ["completed", "failed", "gated"]);
      assert.equal(sa.status, "completed");
      assert.equal(sa.steps.approve?.verdict, "auto-approved (control-mode synchronous)");
      const ea = readEvents(r.rt.requireRunDir(a.run_id));
      assert.ok(ea.some((e) => e.type === "warn" && /auto-approved/.test(e.message ?? "")));
      assert.equal(
        ea.some((e) => e.type === "gate.opened"),
        false,
      );

      const b = await exec.handlers.run(
        {
          workflow: "approval",
          cwd: r.cwd,
          answers: { "control-mode": "synchronous" },
          context: {},
          inputs: {},
        },
        ctx,
      );
      const sb = await untilStatus(r, b.run_id, ["completed", "failed", "gated"]);
      assert.equal(sb.status, "completed");
    },
  );

  // ---- failures ----------------------------------------------------------------------------------------

  test("timeout result fails the step and the run with the reason", async () => {
    const r = mkRoot();
    const claude = claudeFake(() => ({
      text: "",
      usage: usage(7, 1),
      exit: "timeout",
      error: "timed out",
    }));
    const exec = make(r, { adapters: { claude } });
    const { run_id } = await exec.handlers.run(
      { workflow: "single-agent", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    const state = await untilStatus(r, run_id, ["completed", "failed"]);
    assert.equal(state.status, "failed");
    assert.equal(state.steps.answer?.status, "failed");
    assert.equal(state.steps.answer?.error, "timed out");
    assert.equal(state.steps.answer?.verdict, "timeout: timed out");
    assert.equal(state.error, "answer: timed out");
    assert.equal(state.usage.subscription.input, 7, "usage is folded even on failure");
  });

  test("missing output name fails the step: schema result lacks <name>", async () => {
    const r = mkRoot();
    const claude = claudeFake(() => ({
      text: "hi",
      json: { other: 1 },
      usage: usage(),
      exit: "ok",
    }));
    const exec = make(r, { adapters: { claude } });
    const { run_id } = await exec.handlers.run(
      { workflow: "single-agent", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    const state = await untilStatus(r, run_id, ["completed", "failed"]);
    assert.equal(state.status, "failed");
    assert.equal(state.steps.answer?.error, "schema result lacks answer");
    assert.equal(state.outputs.answer, undefined);
  });

  test("auth probe failure throws AUTH_REQUIRED before any run dir exists", async () => {
    const r = mkRoot();
    const claude = fakeAdapter("claude", (req) => schemaAnswer(req), {
      loggedIn: false,
      loginCmd: "claude auth login",
    });
    const exec = make(r, { adapters: { claude } });
    const err = await attempt(() =>
      exec.handlers.run(
        { workflow: "single-agent", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
        ctx,
      ),
    );
    assert.equal(domainCode(err), "AUTH_REQUIRED");
    const data = (err as RpcError).data as { harness: string; login_cmd: string };
    assert.equal(data.harness, "claude");
    assert.equal(data.login_cmd, "claude auth login");
    assert.deepEqual(r.rt.listRunDirs(), []);
    assert.equal(existsSync(r.rt.paths.runsRoot), false);

    // A harness without an adapter fails the same way, with its login command.
    const exec2 = make(r, { adapters: { claude: claudeFake() } });
    writeFileSync(
      join(r.root, "codex.yaml"),
      "version: 2\nname: codex\nsteps:\n  - id: a\n    type: agent\n    harness: codex\n    model: gpt\n    prompt: hi\n",
    );
    const err2 = await attempt(() =>
      exec2.handlers.run(
        { workflow: join(r.root, "codex.yaml"), cwd: r.cwd, answers: {}, context: {}, inputs: {} },
        ctx,
      ),
    );
    assert.equal(domainCode(err2), "AUTH_REQUIRED");
    assert.equal(((err2 as RpcError).data as { harness: string }).harness, "codex");
    assert.equal(((err2 as RpcError).data as { login_cmd: string }).login_cmd, "codex login");
    assert.deepEqual(r.rt.listRunDirs(), []);
  });

  test("auth exit from a child fails the run with AUTH_REQUIRED in state.error", async () => {
    const r = mkRoot();
    const claude = claudeFake(() => ({
      text: "",
      usage: usage(0, 0),
      exit: "auth",
      error: "not logged in",
    }));
    const exec = make(r, { adapters: { claude } });
    const { run_id } = await exec.handlers.run(
      { workflow: "single-agent", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    const state = await untilStatus(r, run_id, ["completed", "failed"]);
    assert.equal(state.status, "failed");
    assert.match(state.error ?? "", /^AUTH_REQUIRED: claude .*claude auth login/);
    assert.equal(types(readEvents(r.rt.requireRunDir(run_id))).at(-1), "run.failed");
  });

  // ---- rate limits (E12) -----------------------------------------------------------------------------------

  test("rate_limited backs off, warns, falls back to the group's fallback harness and succeeds", async () => {
    const r = mkRoot();
    const claude = claudeFake(() => ({
      text: "",
      usage: usage(5, 0),
      exit: "rate_limited",
      error: "429 overloaded",
    }));
    const codex = fakeAdapter("codex", (req) => schemaAnswer(req, { usage: usage(50, 5) }));
    const exec = make(r, { adapters: { claude, codex } });
    const { run_id } = await exec.handlers.run(
      { workflow: "fallback", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    const state = await untilStatus(r, run_id, ["completed", "failed"]);
    assert.equal(state.status, "completed");
    assert.equal(claude.calls.length, 1);
    assert.equal(codex.calls.length, 1);
    assert.equal(state.steps.answer?.attempts, 2);
    assert.equal(state.steps.answer?.resolved?.harness, "codex");
    assert.equal(state.usage.by_harness.claude?.input, 5);
    assert.equal(state.usage.by_harness.codex?.input, 50);
    assert.equal(state.usage.subscription.input, 55);
    const events = readEvents(r.rt.requireRunDir(run_id));
    const warns = events.filter((e) => e.type === "warn").map((e) => e.message ?? "");
    assert.ok(
      warns.some((m) => /rate limited on claude; backoff/.test(m)),
      warns.join(" | "),
    );
    assert.ok(
      warns.some((m) => m === "answer falls back to codex"),
      warns.join(" | "),
    );
    const starts = events.filter((e) => e.type === "step.started").map((e) => e.harness);
    assert.deepEqual(starts, ["claude", "codex"]);
  });

  test("rate_limited with a fallback that has no adapter warns and retries the same harness after backoff", async () => {
    const r = mkRoot();
    const claude = claudeFake((req, n) =>
      n === 1
        ? { text: "", usage: usage(1, 0), exit: "rate_limited", error: "rate limit" }
        : schemaAnswer(req as never),
    );
    const exec = make(r, { adapters: { claude } });
    const t0 = Date.now();
    const { run_id } = await exec.handlers.run(
      { workflow: "fallback-missing", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    const state = await untilStatus(r, run_id, ["completed", "failed"]);
    assert.equal(state.status, "completed");
    assert.equal(claude.calls.length, 2);
    assert.ok(Date.now() - t0 >= 20, "waited the injected backoff");
    const warns = readEvents(r.rt.requireRunDir(run_id))
      .filter((e) => e.type === "warn")
      .map((e) => e.message ?? "");
    assert.ok(
      warns.some((m) => /no adapter for fallback gemini; waiting for claude/.test(m)),
      warns.join(" | "),
    );
  });

  test("E12 fallback end to end: claude parked for the first backoff, codex probed lazily and dispatched with the same prompt and schema on a fresh cursor", async () => {
    const r = mkRoot();
    const t = fakeTimers();
    const claude = claudeFake(() => RATE_LIMITED);
    const codex = fakeAdapter("codex", (req) =>
      schemaAnswer(req, { usage: usage(50, 5), cursor: "codex-thread" }),
    );
    const exec = make(r, {
      adapters: { claude, codex },
      backoffMs: defaultBackoffMs,
      channel: { timers: t },
    });
    const { run_id } = await exec.handlers.run(
      { workflow: "fallback", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    const state = await untilStatus(r, run_id, ["completed", "failed"]);
    assert.equal(state.status, "completed");
    assert.equal(claude.calls.length, 1);
    assert.equal(codex.calls.length, 1);
    // Run start probed the primary harness only; the fallback was probed once, on first use.
    assert.deepEqual(claude.probes, ["subscription"]);
    assert.deepEqual(codex.probes, ["subscription"]);
    const [first] = claude.calls;
    const [second] = codex.calls;
    assert.ok(first && second);
    assert.equal(second.prompt, first.prompt);
    assert.deepEqual(second.schema, first.schema);
    assert.equal(second.resume, undefined, "no cursor crosses harnesses");
    assert.equal(first.model, "haiku");
    assert.equal(second.model, "inherit", "the fallback harness runs its own default model");
    assert.equal(second.effort, first.effort, "effort carries over (haiku drops it on both)");
    // Step state carries the harness that finished the step.
    const answer = state.steps.answer;
    assert.equal(answer?.attempts, 2);
    assert.equal(answer?.resolved?.harness, "codex");
    assert.equal(answer?.resolved?.model, "inherit");
    assert.match(answer?.resolved?.reason ?? "", /fallback from claude/);
    assert.equal(answer?.cursor, "codex-thread");
    assert.equal(state.usage.by_harness.claude?.input, 5);
    assert.equal(state.usage.by_harness.codex?.input, 50);
    assert.equal(state.usage.subscription.input, 55);
    // Events: warn on the park, warn on the switch, harness on start / done / usage.
    const events = readEvents(r.rt.requireRunDir(run_id));
    const warns = warnsOf(events);
    assert.ok(
      warns.some((m) => m === "rate limited on claude; backoff 60s: 429 overloaded"),
      warns.join(" | "),
    );
    assert.ok(warns.includes("answer falls back to codex"), warns.join(" | "));
    const starts = events.filter((e) => e.type === "step.started").map((e) => e.harness);
    assert.deepEqual(starts, ["claude", "codex"]);
    const done = events.filter((e) => e.type === "step.done" && e.step === "answer");
    assert.equal(done.length, 1);
    assert.equal(done[0]?.harness, "codex");
    const usages = events.filter((e) => e.type === "usage").map((e) => [e.harness, e.usage?.input]);
    assert.deepEqual(usages, [
      ["claude", 5],
      ["codex", 50],
    ]);
    // Claude stays parked for the whole first backoff on the engine clock (1 min), then frees.
    assert.equal(t.pending(), 1, "the park timer is the only live timer");
    assert.ok(exec.isBusy(), "a parked harness keeps the daemon busy");
    t.advance(59_000);
    assert.ok(exec.isBusy());
    t.advance(1_000);
    assert.equal(t.pending(), 0);
    assert.equal(exec.isBusy(), false);
  });

  test("E12 fallback harness logged out: warn once, no dispatch there, the step waits for its primary", async () => {
    const r = mkRoot();
    const claude = claudeFake((req, n) => (n === 1 ? RATE_LIMITED : schemaAnswer(req as never)));
    const codex = fakeAdapter("codex", (req) => schemaAnswer(req), {
      loggedIn: false,
      loginCmd: "codex login",
    });
    const exec = make(r, { adapters: { claude, codex } });
    const { run_id } = await exec.handlers.run(
      { workflow: "fallback", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    const state = await untilStatus(r, run_id, ["completed", "failed"]);
    assert.equal(state.status, "completed");
    assert.equal(claude.calls.length, 2);
    assert.equal(codex.calls.length, 0);
    assert.equal(codex.probes.length, 1, "probed once, lazily; the run still started");
    assert.equal(state.steps.answer?.resolved?.harness, "claude");
    const events = readEvents(r.rt.requireRunDir(run_id));
    const warns = warnsOf(events);
    assert.equal(
      warns.filter(
        (m) => m === "answer: fallback codex not logged in; run `codex login`; waiting for claude",
      ).length,
      1,
      warns.join(" | "),
    );
    assert.equal(warns.includes("answer falls back to codex"), false);
    const starts = events.filter((e) => e.type === "step.started").map((e) => e.harness);
    assert.deepEqual(starts, ["claude", "claude"]);
  });

  test("E12 both harnesses rate-limited: the step waits for whichever backoff expires first", async () => {
    const r = mkRoot();
    const t = fakeTimers();
    const claude = claudeFake((req, n) => (n === 1 ? RATE_LIMITED : schemaAnswer(req as never)));
    // The codex child runs long enough for the test to move the engine clock while it is live.
    const codex = fakeAdapter(
      "codex",
      () => ({ text: "", usage: usage(1, 0), exit: "rate_limited", error: "usage limit" }),
      { delayMs: 80 },
    );
    const exec = make(r, {
      adapters: { claude, codex },
      backoffMs: defaultBackoffMs,
      channel: { timers: t },
    });
    const { run_id } = await exec.handlers.run(
      { workflow: "fallback", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    const runDir = r.rt.requireRunDir(run_id);
    await until(() => codex.calls.length === 1, "codex dispatch");
    // claude parked at T0 until T0+60s; codex parks at T0+10s until T0+70s.
    t.advance(10_000);
    await until(
      () => warnsOf(readEvents(runDir)).some((m) => /rate limited on codex; backoff 60s/.test(m)),
      "codex parked",
    );
    await pause(40);
    assert.equal(readState(runDir).status, "running");
    assert.equal(readState(runDir).steps.answer?.status, "pending");
    assert.equal(claude.calls.length, 1);
    assert.equal(codex.calls.length, 1);
    assert.equal(t.pending(), 2, "two park timers, nothing else live");
    // T0+60s: claude frees first and takes the step back; codex stays parked until T0+70s.
    t.advance(50_000);
    const state = await untilStatus(r, run_id, ["completed", "failed"]);
    assert.equal(state.status, "completed");
    assert.equal(claude.calls.length, 2);
    assert.equal(codex.calls.length, 1);
    assert.equal(state.steps.answer?.resolved?.harness, "claude");
    assert.equal(state.steps.answer?.resolved?.model, "haiku", "primary resolution restored");
    assert.equal(state.steps.answer?.attempts, 3);
    const starts = readEvents(runDir)
      .filter((e) => e.type === "step.started")
      .map((e) => e.harness);
    assert.deepEqual(starts, ["claude", "codex", "claude"]);
    assert.equal(t.pending(), 1, "the codex park is still armed");
    t.advance(10_000);
    assert.equal(t.pending(), 0);
  });

  test("E12 cursor guard: a stored claude cursor resumes on claude but is never handed to the codex fallback", async () => {
    const r = mkRoot();
    // First daemon life: the step starts on claude, records a session cursor, then the daemon dies.
    const { starter, held } = heldStarter({ killResolves: false });
    const exec1 = make(r, { startAgent: starter, adapters: { claude: claudeFake() } });
    const { run_id } = await exec1.handlers.run(
      { workflow: "fallback-resume", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
      ctx,
    );
    await until(() => held.length === 1, "first dispatch");
    assert.equal(held[0]?.harness, "claude");
    assert.equal(held[0]?.req.resume, undefined);
    const runDir = r.rt.requireRunDir(run_id);
    updateStep(runDir, "answer", { cursor: "claude-sess" });
    exec1.stop();
    resetRunning(runDir);
    // Second life: claude resumes the session, gets rate limited, codex starts fresh.
    const claude = claudeFake((req, n) => (n === 1 ? RATE_LIMITED : schemaAnswer(req as never)));
    const codex = fakeAdapter("codex", (req) => schemaAnswer(req, { cursor: "codex-thread" }));
    const exec2 = make(r, { adapters: { claude, codex } });
    assert.deepEqual(exec2.pickUp(), [run_id]);
    const state = await untilStatus(r, run_id, ["completed", "failed"]);
    assert.equal(state.status, "completed");
    assert.equal(claude.calls[0]?.resume, "claude-sess", "same harness: the cursor resumes");
    assert.equal(codex.calls[0]?.resume, undefined, "other harness: fresh session");
    assert.equal(state.steps.answer?.cursor, "codex-thread");
    assert.equal(state.steps.answer?.resolved?.harness, "codex");
  });

  test(
    "real starter: codex and grok children carry pid and kill; child record written; cancel kills the process",
    { timeout: 30_000 },
    async () => {
      // A fake CLI that answers the codex login probe and otherwise runs until killed.
      const script = `
        const args = process.argv.slice(2);
        if (args[0] === "login") { process.stdout.write("Logged in using ChatGPT\\n"); process.exit(0); }
        setInterval(() => {}, 1000);
      `;
      for (const harness of ["codex", "grok"] as const) {
        const r = mkRoot();
        const binDir = fakeCli(harness, script);
        const grokHome = join(r.root, "grok-home");
        mkdirSync(grokHome);
        writeFileSync(join(grokHome, "auth.json"), JSON.stringify({ token: "x" }));
        await withEnv(
          { PATH: `${binDir}:${process.env.PATH ?? ""}`, GROK_HOME: grokHome },
          async () => {
            const exec = make(r, {
              adapters: { codex: codexAdapter, grok: grokAdapter },
              channel: { inject: false },
            });
            const { run_id } = await exec.handlers.run(
              { workflow: `direct-${harness}`, cwd: r.cwd, answers: {}, context: {}, inputs: {} },
              ctx,
            );
            const runDir = r.rt.requireRunDir(run_id);
            await until(() => readChild(runDir) !== null, `${harness} child record`);
            const rec = readChild(runDir);
            assert.ok(rec && rec.pid > 0, "pid comes from the real starter");
            assert.ok(pidAlive(rec.pid));
            assert.ok(exec.isBusy());
            const started = readEvents(runDir).find((e) => e.type === "step.started");
            assert.equal(started?.harness, harness);
            assert.equal(started?.model, "inherit");
            exec.handlers.cancel({ run_id }, ctx);
            await until(() => !pidAlive(rec.pid), `${harness} child killed`, 5000);
            assert.equal(readState(runDir).status, "cancelled");
            assert.equal(exec.nudge(run_id, "answer", "hi"), false, "no stdin nudge off Claude");
          },
        );
      }
    },
  );

  test(
    "live: a claude rate limit routes the step to real codex (WISE_LIVE=1)",
    { skip: process.env.WISE_LIVE !== "1", timeout: 300_000 },
    async () => {
      // A `claude` shim ahead of the real one on PATH: passes the auth probe, then answers the
      // prompt with a rate-limit result event so the real parser classifies it `rate_limited`.
      const script = `
        const args = process.argv.slice(2);
        const out = (o) => process.stdout.write(JSON.stringify(o) + "\\n");
        if (args[0] === "auth" && args[1] === "status") { out({ loggedIn: true }); process.exit(0); }
        out({ type: "system", subtype: "init", session_id: "fake-claude-sess", model: "haiku", tools: [] });
        out({ type: "result", subtype: "error_during_execution", is_error: true,
              result: "Rate limit reached for this plan window (429)",
              usage: { input_tokens: 5, output_tokens: 0 } });
        process.exit(0);
      `;
      const r = mkRoot();
      const binDir = fakeCli("claude", script);
      await withEnv({ PATH: `${binDir}:${process.env.PATH ?? ""}` }, async () => {
        const exec = make(r, {
          backoffMs: defaultBackoffMs,
          channel: { inject: false },
          defaultTimeoutMs: 240_000,
        });
        const t0 = Date.now();
        const { run_id } = await exec.handlers.run(
          { workflow: "fallback", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
          ctx,
        );
        const runDir = r.rt.requireRunDir(run_id);
        await until(
          () => ["completed", "failed"].includes(readState(runDir).status),
          "live run end",
          280_000,
        );
        const state = readState(runDir);
        const events = readEvents(runDir);
        console.log(`live fallback: ${state.status} in ${Math.round((Date.now() - t0) / 1000)}s`);
        for (const e of events) {
          const tag = e.harness ? ` [${e.harness}]` : "";
          console.log(
            `  ${e.type}${e.step ? `:${e.step}` : ""}${tag} ${e.message ?? e.verdict ?? ""}`,
          );
        }
        console.log(
          "  outputs",
          JSON.stringify(state.outputs),
          "usage",
          JSON.stringify(state.usage),
        );
        assert.equal(state.status, "completed", state.error);
        const starts = events.filter((e) => e.type === "step.started").map((e) => e.harness);
        assert.deepEqual(starts, ["claude", "codex"]);
        assert.equal(state.steps.answer?.resolved?.harness, "codex");
        assert.equal(typeof state.outputs.answer, "string");
        assert.ok((state.usage.by_harness.codex?.input ?? 0) > 0);
        assert.equal(state.usage.by_harness.claude?.input, 5);
        const warns = warnsOf(events);
        assert.ok(
          warns.some((m) => /rate limited on claude; backoff 60s/.test(m)),
          warns.join(" | "),
        );
        assert.ok(warns.includes("answer falls back to codex"), warns.join(" | "));
        exec.stop();
      });
    },
  );

  // ---- concurrency -------------------------------------------------------------------------------------------

  test(
    "concurrency cap: three ready claude steps, cap 2, at most two in flight",
    { timeout: 30_000 },
    async () => {
      const r = mkRoot();
      const claude = fakeAdapter("claude", (req) => schemaAnswer(req), { delayMs: 80 });
      const exec = make(r, {
        adapters: { claude },
        concurrency: { harness: { claude: 2 }, global: 4 },
      });
      const { run_id } = await exec.handlers.run(
        { workflow: "three-parallel", cwd: r.cwd, answers: {}, context: {}, inputs: {} },
        ctx,
      );
      await until(() => claude.inFlight > 0, "first dispatch");
      assert.ok(exec.isBusy());
      const state = await untilStatus(r, run_id, ["completed", "failed"]);
      assert.equal(state.status, "completed");
      assert.equal(claude.calls.length, 3);
      assert.equal(claude.maxInFlight, 2);
      assert.deepEqual(state.outputs, { a: "a-value", b: "b-value", c: "c-value" });

      // global cap 1 serialises everything
      const r2 = mkRoot();
      const claude2 = fakeAdapter("claude", (req) => schemaAnswer(req), { delayMs: 30 });
      const exec2 = make(r2, { adapters: { claude: claude2 }, concurrency: { global: 1 } });
      const b = await exec2.handlers.run(
        { workflow: "three-parallel", cwd: r2.cwd, answers: {}, context: {}, inputs: {} },
        ctx,
      );
      await untilStatus(r2, b.run_id, ["completed", "failed"]);
      assert.equal(claude2.maxInFlight, 1);
    },
  );

  // ---- resume / pick-up / cancel ------------------------------------------------------------------------------

  test("resume after a simulated crash: running step reset, run completes", async () => {
    const r = mkRoot();
    const runId = newUlid();
    const runDir = join(r.rt.paths.runsRoot, "ws", runId);
    initState({
      runDir,
      runId,
      workflow: { name: "single-agent", version: 2, dir: "" },
      stepIds: ["answer"],
      cwd: r.cwd,
    });
    startRun(runDir, {
      resolved: { answer: { harness: "claude", model: "haiku", effort: "low" } },
    });
    startStep(runDir, "answer");
    // What crash recovery leaves behind: paused, step still marked running.
    updateRun(runDir, { status: "paused" });
    const claude = claudeFake();
    const exec = make(r, { adapters: { claude } });
    const res = await exec.handlers.resume({ run_id: runId }, ctx);
    assert.deepEqual(res, { run_id: runId, status: "running" });
    const state = await untilStatus(r, runId, ["completed", "failed"]);
    assert.equal(state.status, "completed");
    assert.equal(state.steps.answer?.attempts, 2);
    assert.equal(state.outputs.answer, "answer-value");
    assert.equal(claude.calls.length, 1);
    const seq = types(readEvents(runDir));
    assert.ok(seq.includes("warn"), "ledger resume warn");
    assert.equal(seq.at(-1), "run.done");
  });

  test("pickUp continues `running` runs with pending steps; paused runs are left alone", async () => {
    const r = mkRoot();
    const mk = (status: "running" | "paused"): string => {
      const runId = newUlid();
      const runDir = join(r.rt.paths.runsRoot, "ws", runId);
      initState({
        runDir,
        runId,
        workflow: { name: "single-agent", version: 2, dir: "" },
        stepIds: ["answer"],
        cwd: r.cwd,
      });
      startRun(runDir, {});
      updateRun(runDir, { status });
      return runId;
    };
    const running = mk("running");
    const paused = mk("paused");
    const exec = make(r, { adapters: { claude: claudeFake() } });
    assert.deepEqual(exec.pickUp(), [running]);
    const state = await untilStatus(r, running, ["completed", "failed"]);
    assert.equal(state.status, "completed");
    assert.equal(readState(r.rt.requireRunDir(paused)).status, "paused");
  });

  test("cancel kills the live bash child and stops the loop", { timeout: 30_000 }, async () => {
    const r = mkRoot();
    const exec = make(r, { adapters: { claude: claudeFake() } });
    const pidFile = join(r.root, "slow.pid");
    const { run_id } = await exec.handlers.run(
      {
        workflow: "slow-bash",
        cwd: r.cwd,
        answers: {},
        context: {},
        inputs: { pid_file: pidFile },
      },
      ctx,
    );
    await until(() => existsSync(pidFile), "bash child started");
    const pid = Number(readFileSync(pidFile, "utf8").trim());
    assert.ok(pidAlive(pid));
    assert.deepEqual(await exec.handlers.cancel({ run_id, reason: "test" }, ctx), {
      status: "cancelled",
    });
    const state = readState(r.rt.requireRunDir(run_id));
    assert.equal(state.status, "cancelled");
    assert.equal(state.steps.slow?.status, "cancelled");
    await until(() => !pidAlive(pid), "bash child killed", 5000);
    assert.equal(exec.liveRuns().length, 0);
    await pause(50);
    assert.equal(
      readState(r.rt.requireRunDir(run_id)).status,
      "cancelled",
      "late completion does not overwrite",
    );
  });

  // ---- through the daemon -------------------------------------------------------------------------------------

  test(
    "end to end through startDaemon + RPC client: preflight, run, wait loop, answers, report",
    { timeout: 30_000 },
    async () => {
      const r = mkRoot();
      let exec: Executor | undefined;
      const claude = claudeFake();
      const daemon = await startDaemon({
        env: r.env,
        version: VERSION,
        idleMs: 60_000,
        wait: { pollMs: 15 },
        handlers: executorHandlers(
          {
            env: r.env,
            roots: { userRoot: EXEC_FIXTURES, bundledRoot: DEFS },
            adapters: { claude },
            configPath: join(r.root, "engine.json"),
          },
          (e) => {
            exec = e;
          },
        ),
        isBusy: () => exec?.isBusy() ?? false,
      });
      daemons.add(daemon);
      assert.ok(exec);
      executors.add(exec);
      const c = await connect({ env: r.env, version: VERSION, client: "test", timeoutMs: 10_000 });
      clients.add(c);

      const pre = await c.call("preflight", { workflow: EXAMPLE, cwd: r.cwd });
      assert.equal(pre.questions.length, 4);
      const { run_id } = await c.call("run", {
        workflow: EXAMPLE,
        cwd: r.cwd,
        answers: { ...pre.defaults, profile: "max" },
        context: { guidance: "keep it short" },
        inputs: {},
      });
      assert.equal(daemon.activeRuns(), 1);

      const seen: Event[] = [];
      const answers: string[] = [];
      let since = 0;
      for (let i = 0; i < 60; i++) {
        const res = await c.call(
          "wait",
          { run_id, after: since, timeout_ms: 5000 },
          { timeoutMs: 10_000 },
        );
        seen.push(...res.events);
        since = res.events.at(-1)?.seq ?? since;
        if (res.done) break;
        if (res.status === "gated" && res.gate) {
          const value = res.gate.kind === "ask" ? "docs" : "approve";
          answers.push(`${res.gate.step}=${value}`);
          await c.call("answer", { run_id, gate_id: res.gate.gate_id, value });
        }
      }
      assert.deepEqual(answers, ["pick-next=docs", "approve-summary=approve"]);
      const status = await c.call("status", { run_id });
      assert.equal((status as { status: string }).status, "completed");
      const report = await c.call("report", { run_id });
      assert.equal(report.verdicts["pick-next"], "next_focus=docs");
      assert.equal(report.usage.subscription.input, 300);
      assert.equal(types(seen)[0], "run.started");
      assert.equal(types(seen).at(-1), "run.done");
      // `max` profile swaps the summarize group to sonnet / medium; classify keeps haiku / low.
      const summarize = claude.calls.find((x) => x.prompt.includes("summarise"));
      assert.equal(summarize?.model, "sonnet");
      assert.equal(summarize?.effort, "medium");
      assert.equal(claude.calls.find((x) => x.prompt.includes("Classify"))?.model, "haiku");
      assert.match(
        summarize?.prompt ?? "",
        /Focus: keep it short/,
        "from-context guidance pre-filled the input",
      );
      c.close();
      await daemon.close();
    },
  );
});
