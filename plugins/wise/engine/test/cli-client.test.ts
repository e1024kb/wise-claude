import { after, before, beforeEach, describe, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";
import { clientCommand, fillAnswers, formatEvent } from "../src/cli-client.ts";
import { stopDaemon } from "../src/client.ts";
import { daemonPaths, startDaemon } from "../src/daemon.ts";
import type { Daemon } from "../src/daemon.ts";
import type { PreflightResult, WaitResult } from "../src/protocol.ts";
import { domainError } from "../src/rpc.ts";
import type { RpcError } from "../src/rpc.ts";
import type { Event, Gate, Question, RunSummary } from "../src/types.ts";

type Call = { method: string; params: unknown };
type Fake = {
  calls: Call[];
  waits: WaitResult[];
  preflight: PreflightResult;
  fail: Partial<Record<string, RpcError>>;
};

const QUESTIONS: Question[] = [
  {
    id: "profile",
    kind: "choice",
    label: "Budget profile?",
    options: [
      { value: "low", label: "low" },
      { value: "medium", label: "medium" },
    ],
    default: "medium",
  },
  {
    id: "tuning.plan",
    kind: "choice",
    label: "Plan tuning",
    options: [{ value: "default", label: "default" }],
    default: "default",
    locked: true,
  },
  { id: "input.ticket", kind: "text", label: "Ticket ref?" },
  { id: "input.notes", kind: "text", label: "Notes?", optional: true },
];

const RUN_ID = "01RUN";

function ev(seq: number, type: Event["type"], extra: Partial<Event> = {}): Event {
  return { seq, ts: `2026-09-05T10:00:0${seq}.000Z`, run_id: RUN_ID, type, ...extra };
}

const APPROVAL: Gate = {
  gate_id: "g1",
  step: "approve",
  kind: "approval",
  message: "Ship it?",
  options: [
    { value: "approve", label: "Approve" },
    { value: "reject", label: "Reject" },
  ],
};

const SUMMARY: RunSummary = {
  run_id: RUN_ID,
  workflow: "wf",
  status: "running",
  started_at: "2026-09-05T10:00:00.000Z",
  last_activity_at: "2026-09-05T10:00:01.000Z",
  cwd: "/tmp/x",
};

function freshFake(): Fake {
  return {
    calls: [],
    waits: [],
    preflight: {
      workflow: "wf",
      version: 2,
      questions: QUESTIONS,
      defaults: {},
      requires_missing: [],
    },
    fail: {},
  };
}

type Run = { code: number; out: string; err: string; lines: string[] };

const roots: string[] = [];

function mkRoot(): { root: string; env: Record<string, string> } {
  const root = mkdtempSync(join(tmpdir(), "wc-"));
  roots.push(root);
  const env = { XDG_DATA_HOME: root, HOME: root };
  assert.ok(daemonPaths({ env }).socketPath.length < 100);
  return { root, env };
}

describe("cli-client", () => {
  const main = mkRoot();
  const env = main.env;
  let daemon: Daemon;
  let fake: Fake = freshFake();

  function failing(method: string): void {
    const e = fake.fail[method];
    if (e) throw e;
  }

  before(async () => {
    daemon = await startDaemon({
      env,
      idleMs: 60_000,
      handlers: {
        preflight: (p) => {
          fake.calls.push({ method: "preflight", params: p });
          failing("preflight");
          return fake.preflight;
        },
        run: (p) => {
          fake.calls.push({ method: "run", params: p });
          failing("run");
          return { run_id: RUN_ID, status: "running" };
        },
        wait: (p) => {
          fake.calls.push({ method: "wait", params: p });
          const next = fake.waits.shift();
          if (!next) throw domainError("RUN_NOT_FOUND", "script exhausted");
          return next;
        },
        answer: (p) => {
          fake.calls.push({ method: "answer", params: p });
          return { accepted: true };
        },
        status: (p) => {
          fake.calls.push({ method: "status", params: p });
          return p.run_id ? { ...SUMMARY, run_id: p.run_id } : [SUMMARY];
        },
        cancel: (p) => {
          fake.calls.push({ method: "cancel", params: p });
          failing("cancel");
          return { status: "cancelled" };
        },
        resume: (p) => {
          fake.calls.push({ method: "resume", params: p });
          return { run_id: p.run_id, status: "running" };
        },
        report: (p) => {
          fake.calls.push({ method: "report", params: p });
          return {
            units: [],
            usage: {
              subscription: {
                input: 1500,
                output: 200,
                cache_read: 0,
                cache_write: 0,
                pool: "subscription",
              },
              "api-key": { input: 0, output: 0, cache_read: 0, cache_write: 0, pool: "api-key" },
              by_harness: {
                claude: {
                  input: 1500,
                  output: 200,
                  cache_read: 0,
                  cache_write: 0,
                  pool: "subscription",
                },
              },
              by_step: {
                classify: {
                  input: 1500,
                  output: 200,
                  cache_read: 0,
                  cache_write: 0,
                  pool: "subscription",
                },
              },
            },
            usage_total: {
              input: 1500,
              output: 200,
              cache_read: 0,
              cache_write: 0,
              pool: "subscription",
            },
            resolved: { classify: { harness: "claude", model: "haiku", effort: "" } },
            verdicts: { classify: "bug" },
          };
        },
      },
    });
  });

  beforeEach(() => {
    fake = freshFake();
  });

  after(async () => {
    await daemon.close();
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  /** `--no-start` by default so a broken test never spawns a real daemon. */
  async function run(
    argv: string[],
    stdin: string[] = [],
    useEnv: Record<string, string> = env,
    autoStart = false,
  ): Promise<Run> {
    let out = "";
    let err = "";
    const code = await clientCommand(autoStart ? argv : [...argv, "--no-start"], {
      out: (s) => (out += s),
      err: (s) => (err += s),
      env: useEnv,
      stdin: Readable.from(stdin),
    });
    return { code, out, err, lines: out.split("\n").filter((l) => l !== "") };
  }

  function calls(method: string): Call[] {
    return fake.calls.filter((c) => c.method === method);
  }

  test("usage: unknown command and missing positionals exit 64 without touching the daemon", async () => {
    assert.equal((await run(["frobnicate"])).code, 64);
    const r = await run(["run"]);
    assert.equal(r.code, 64);
    assert.match(r.err, /missing <workflow>/);
    assert.equal((await run(["answer", RUN_ID, "g1"])).code, 64);
    assert.equal((await run(["wait"])).code, 64);
    assert.equal(fake.calls.length, 0);
  });

  test("run: a required input without default or answer exits 64 and never calls run", async () => {
    const r = await run(["run", "wf", "--cwd", "/tmp/x"]);
    assert.equal(r.code, 64, r.err);
    const j = JSON.parse(r.out) as { error: { code: string; missing: string[] } };
    assert.equal(j.error.code, "MISSING_ANSWERS");
    assert.deepEqual(j.error.missing, ["input.ticket"]);
    assert.equal(calls("preflight").length, 1);
    assert.equal(calls("run").length, 0);

    const t = await run(["run", "wf", "--text"]);
    assert.equal(t.code, 64);
    assert.match(t.err, /no interactive prompting/);
    assert.match(t.err, /input\.ticket/);
    assert.equal(t.out, "");
  });

  test("run: --profile and --input fill answers, defaults fill the rest, locked stay untouched", async () => {
    const r = await run([
      "run",
      "wf",
      "--cwd",
      "/tmp/x",
      "--profile",
      "low",
      "--input",
      "ticket=LEC-1",
      "--context",
      '{"guidance":"be brief"}',
    ]);
    assert.equal(r.code, 0, r.err + r.out);
    const [call] = calls("run");
    assert.ok(call);
    const params = call.params as {
      workflow: string;
      cwd: string;
      answers: Record<string, unknown>;
      inputs: Record<string, string>;
      context: { guidance?: string };
    };
    assert.equal(params.workflow, "wf");
    assert.equal(params.cwd, "/tmp/x");
    assert.deepEqual(params.answers, { profile: "low", "input.ticket": "LEC-1" });
    assert.deepEqual(params.inputs, { ticket: "LEC-1" });
    assert.equal(params.context.guidance, "be brief");
    const j = JSON.parse(r.out) as { run_id: string; status: string };
    assert.equal(j.run_id, RUN_ID);
    assert.equal(j.status, "running");
    const pre = calls("preflight")[0]?.params as { workflow: string; cwd: string };
    assert.deepEqual(pre, { workflow: "wf", cwd: "/tmp/x" });
  });

  test("run: --answers merges (explicit beats default), bad JSON and bad --profile exit 64", async () => {
    const r = await run(["run", "wf", "--answers", '{"input.ticket":"LEC-2","input.notes":"n"}']);
    assert.equal(r.code, 0, r.err);
    const params = calls("run")[0]?.params as { answers: Record<string, unknown> };
    assert.deepEqual(params.answers, {
      profile: "medium",
      "input.ticket": "LEC-2",
      "input.notes": "n",
    });
    assert.equal((await run(["run", "wf", "--answers", "{nope"])).code, 64);
    assert.equal((await run(["run", "wf", "--profile", "turbo"])).code, 64);
    assert.equal((await run(["run", "wf", "--input", "novalue"])).code, 64);
  });

  test("fillAnswers: unit semantics", () => {
    const f = fillAnswers(QUESTIONS, {});
    assert.deepEqual(f.missing, ["input.ticket"]);
    assert.deepEqual(f.answers, { profile: "medium" });
    const g = fillAnswers(QUESTIONS, { "input.ticket": "X", profile: "low" });
    assert.deepEqual(g.missing, []);
    assert.deepEqual(g.inputs, { ticket: "X" });
    assert.equal(g.answers["tuning.plan"], undefined);
  });

  test("run --follow --text: events one per line, approval gate answered from stdin, exit 0", async () => {
    fake.waits = [
      {
        events: [
          ev(1, "run.started"),
          ev(2, "step.done", { step: "classify", verdict: "bug, high priority" }),
        ],
        status: "gated",
        gate: APPROVAL,
        done: false,
      },
      {
        events: [ev(3, "gate.answered", { step: "approve" }), ev(4, "run.done")],
        status: "completed",
        done: true,
      },
    ];
    const r = await run(
      ["run", "wf", "--follow", "--text", "--input", "ticket=LEC-1", "--timeout-ms", "5000"],
      ["approve\n"],
    );
    assert.equal(r.code, 0, r.err + r.out);
    assert.ok(r.lines.includes("10:00:02  step.done     classify  bug, high priority"), r.out);
    assert.match(r.out, /^run 01RUN started \(wf\)/m);
    assert.match(r.out, /^GATE g1 \[approval\] step approve$/m);
    assert.match(r.out, /^  Ship it\?$/m);
    assert.match(r.out, /options: approve, reject/);
    assert.match(r.out, /^answered g1: approve$/m);
    assert.match(r.out, /^run completed$/m);
    const answers = calls("answer");
    assert.equal(answers.length, 1);
    assert.deepEqual(answers[0]?.params, { run_id: RUN_ID, gate_id: "g1", value: "approve" });
    const waits = calls("wait").map((c) => c.params as { after: number; timeout_ms: number });
    assert.deepEqual(
      waits.map((w) => w.after),
      [0, 2],
    );
    assert.equal(waits[0]?.timeout_ms, 5000);
  });

  test("run --follow (json): NDJSON records for run, events, gate, answer, done", async () => {
    fake.waits = [
      {
        events: [ev(1, "run.started")],
        status: "gated",
        gate: { gate_id: "g2", step: "pick", kind: "ask", message: "Which?", allow_text: true },
        done: false,
      },
      { events: [ev(2, "run.done")], status: "completed", done: true },
    ];
    const r = await run(["run", "wf", "--follow", "--input", "ticket=LEC-1"], ["free form text\n"]);
    assert.equal(r.code, 0, r.err + r.out);
    const records = r.lines.map((l) => JSON.parse(l) as Record<string, unknown>);
    assert.equal(records[0]?.run_id, RUN_ID);
    assert.equal((records[1] as Event).type, "run.started");
    assert.equal((records[2] as { gate: Gate }).gate.gate_id, "g2");
    assert.equal(records[3]?.answered, "g2");
    assert.equal(records[3]?.value, "free form text");
    assert.equal((records[4] as Event).type, "run.done");
    assert.deepEqual(records[5], { done: true, run_id: RUN_ID, status: "completed" });
  });

  test("run --follow: failed run exits 1 and a gate seen again is not re-asked", async () => {
    fake.waits = [
      { events: [], status: "gated", gate: APPROVAL, done: false },
      { events: [], status: "gated", gate: APPROVAL, done: false },
      { events: [ev(1, "run.failed", { message: "boom" })], status: "failed", done: true },
    ];
    const r = await run(["run", "wf", "--follow", "--input", "ticket=LEC-1"], ["reject\n"]);
    assert.equal(r.code, 1, r.out);
    assert.equal(calls("answer").length, 1);
    assert.equal(calls("wait").length, 3);
    assert.match(r.out, /"status":"failed"/);
  });

  test("run --follow: ask gate with options re-prompts on an invalid line", async () => {
    fake.waits = [
      {
        events: [],
        status: "gated",
        gate: {
          gate_id: "g3",
          step: "pick",
          kind: "ask",
          message: "Which?",
          options: [
            { value: "a", label: "A" },
            { value: "b", label: "B" },
          ],
        },
        done: false,
      },
      { events: [], status: "completed", done: true },
    ];
    const r = await run(
      ["run", "wf", "--follow", "--text", "--input", "ticket=LEC-1"],
      ["zzz\n", "\n", "b\n"],
    );
    assert.equal(r.code, 0, r.out);
    assert.match(r.out, /expected one of: a, b/);
    assert.deepEqual(calls("answer")[0]?.params, { run_id: RUN_ID, gate_id: "g3", value: "b" });
  });

  test("run --follow: stdin closed at a gate exits 1 and names the answer command", async () => {
    fake.waits = [{ events: [], status: "gated", gate: APPROVAL, done: false }];
    const r = await run(["run", "wf", "--follow", "--text", "--input", "ticket=LEC-1"], []);
    assert.equal(r.code, 1);
    assert.match(r.err, /wise-engine answer 01RUN g1 <value>/);
    assert.equal(calls("answer").length, 0);
  });

  test("wait: one call with --after and --timeout-ms forwarded, text and json renderings", async () => {
    fake.waits = [
      {
        events: [ev(7, "step.started", { step: "plan", model: "opus", harness: "claude" })],
        status: "running",
        done: false,
      },
      { events: [], status: "gated", gate: APPROVAL, done: false },
    ];
    const r = await run(["wait", RUN_ID, "--after", "6", "--timeout-ms", "1000"]);
    assert.equal(r.code, 0, r.err);
    assert.deepEqual(calls("wait")[0]?.params, { run_id: RUN_ID, after: 6, timeout_ms: 1000 });
    const j = JSON.parse(r.out) as WaitResult;
    assert.equal(j.events[0]?.seq, 7);
    const t = await run(["wait", RUN_ID, "--text"]);
    assert.equal(t.code, 0);
    assert.match(t.out, /^GATE g1/m);
    assert.match(t.out, /^status: gated$/m);
    assert.equal(((calls("wait")[1] as Call).params as { after?: number }).after, undefined);
  });

  test("status, answer, cancel, resume, report: direct calls with results printed", async () => {
    const all = await run(["status"]);
    assert.equal(all.code, 0, all.err);
    assert.equal((JSON.parse(all.out) as RunSummary[]).length, 1);
    const one = await run(["status", "01OTHER", "--text"]);
    assert.equal(one.code, 0);
    assert.match(one.out, /^01OTHER  wf  running  /);

    const ans = await run(["answer", RUN_ID, "g1", "approve", "--text"]);
    assert.equal(ans.code, 0);
    assert.match(ans.out, /answer accepted for g1/);
    assert.deepEqual(calls("answer")[0]?.params, {
      run_id: RUN_ID,
      gate_id: "g1",
      value: "approve",
    });

    const can = await run(["cancel", RUN_ID, "--reason", "changed my mind"]);
    assert.equal(can.code, 0);
    assert.deepEqual(calls("cancel")[0]?.params, { run_id: RUN_ID, reason: "changed my mind" });
    assert.deepEqual(JSON.parse(can.out), { status: "cancelled" });

    const res = await run(["resume", RUN_ID, "--text"]);
    assert.equal(res.code, 0);
    assert.equal(res.out, `run ${RUN_ID} running\n`);

    const rep = await run(["report", RUN_ID, "--text"]);
    assert.equal(rep.code, 0);
    assert.match(rep.out, /classify: bug/);
    // M6.1 table: step | harness model | in | out | cache_read | cost, then totals by pool / harness.
    assert.match(rep.out, /step\s+harness model\s+in\s+out\s+cache_read\s+cost/);
    assert.match(rep.out, /classify\s+claude haiku\s+1\.5k\s+200\s+0\s+-/);
    assert.match(rep.out, /pool\s+subscription\s+1\.5k\s+200\s+0\s+-/);
    assert.match(rep.out, /harness\s+claude\s+1\.5k\s+200/);
    assert.match(rep.out, /total\s+1\.5k\s+200/);
    assert.doesNotMatch(rep.out, /api-key/, "an empty pool is not listed");
    const repJson = await run(["report", RUN_ID]);
    assert.equal(
      (JSON.parse(repJson.out) as { verdicts: Record<string, string> }).verdicts.classify,
      "bug",
    );
  });

  test("errors: RUN_NOT_FOUND exits 2 with the error envelope; AUTH_REQUIRED shows login_cmd", async () => {
    fake.fail.cancel = domainError("RUN_NOT_FOUND", "no such run: nope", { run_id: "nope" });
    const r = await run(["cancel", "nope"]);
    assert.equal(r.code, 2);
    assert.deepEqual(JSON.parse(r.out), {
      error: { code: "RUN_NOT_FOUND", message: "no such run: nope", run_id: "nope" },
    });
    const t = await run(["cancel", "nope", "--text"]);
    assert.equal(t.code, 2);
    assert.equal(t.out, "");
    assert.match(t.err, /^ERROR RUN_NOT_FOUND: no such run: nope$/m);

    fake.fail.preflight = domainError("AUTH_REQUIRED", "claude needs login", {
      harness: "claude",
      login_cmd: "claude login",
    });
    const a = await run(["run", "wf", "--text"]);
    assert.equal(a.code, 1);
    assert.match(a.err, /^ERROR AUTH_REQUIRED: claude needs login\nclaude login$/m);
    const aj = await run(["run", "wf"]);
    assert.equal(aj.code, 1);
    const j = JSON.parse(aj.out) as { error: Record<string, unknown> };
    assert.equal(j.error.code, "AUTH_REQUIRED");
    assert.equal(j.error.login_cmd, "claude login");
  });

  test("--no-start against a dead socket exits 69 with the /wise-init hint", async () => {
    const dead = mkRoot();
    const r = await run(["status", "--no-start"], [], dead.env);
    assert.equal(r.code, 69);
    const j = JSON.parse(r.out) as { error: { code: string; hint: string } };
    assert.equal(j.error.code, "DAEMON_UNAVAILABLE");
    assert.match(j.error.hint, /wise-init/);
    const t = await run(["status", "--no-start", "--text"], [], dead.env);
    assert.equal(t.code, 69);
    assert.match(t.err, /\/wise-init/);
  });

  test("--socket and --data-root are forwarded into ClientOptions", async () => {
    const other = mkRoot();
    const socketPath = join(other.root, "custom.sock");
    const dataRoot = join(other.root, "data");
    const d2 = await startDaemon({ env: other.env, dataRoot, socketPath, idleMs: 60_000 });
    try {
      const r = await run(
        ["status", "--no-start", "--socket", socketPath, "--data-root", dataRoot],
        [],
        {},
      );
      assert.equal(r.code, 0, r.out + r.err);
      assert.deepEqual(JSON.parse(r.out), []);
    } finally {
      await d2.close();
    }
  });

  test("auto-start: a dead socket spawns the daemon, the call succeeds, then stop it", async () => {
    const fresh = mkRoot();
    try {
      const r = await run(["status"], [], fresh.env, true);
      assert.equal(r.code, 0, r.out + r.err);
      assert.deepEqual(JSON.parse(r.out), []);
    } finally {
      const stopped = await stopDaemon({ env: fresh.env, now: true });
      assert.equal(stopped.stopped, true);
    }
  });

  test("formatEvent: time, padded type, subject, detail", () => {
    assert.equal(
      formatEvent(ev(1, "step.done", { step: "classify", verdict: "ok" })),
      "10:00:01  step.done     classify  ok",
    );
    assert.equal(formatEvent(ev(2, "run.started")), "10:00:02  run.started");
    assert.equal(
      formatEvent(
        ev(3, "usage", {
          step: "plan",
          usage: { input: 2500, output: 40, cache_read: 0, cache_write: 0, pool: "subscription" },
        }),
      ),
      "10:00:03  usage         plan  in 2.5k out 40",
    );
  });
});

test("run --help prints usage and never starts a run", async () => {
  const outLines: string[] = [];
  const code = await clientCommand(["run", "some-workflow", "--help", "--no-start"], {
    out: (s) => outLines.push(s),
    err: () => {},
  });
  assert.equal(code, 0);
  assert.match(outLines.join("\n"), /run <workflow>/);
});
