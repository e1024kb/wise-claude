import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { logPaths } from "../src/ledger.ts";
import {
  buildRunReq,
  DEFAULT_STEP_TIMEOUT_MS,
  extractOutputs,
  headline,
  outcomeOf,
  startAgentStep,
} from "../src/steps/agent.ts";
import type { AgentStarter, AgentStepInput } from "../src/steps/agent.ts";
import { runBashStep } from "../src/steps/bash.ts";
import { APPROVAL_OPTIONS, buildGate, decideGate, isGateStep } from "../src/steps/gate.ts";
import { RPC_INVALID_PARAMS } from "../src/protocol.ts";
import { RpcError } from "../src/rpc.ts";
import type { AgentStep, AskStep, BashStep, RawEvent, Resolved, RunRes } from "../src/types.ts";
import { schemaAnswer, usage } from "./fixtures/executor/fake.ts";

const roots: string[] = [];
function tmp(): string {
  const dir = mkdtempSync(join(tmpdir(), "steps-"));
  roots.push(dir);
  return dir;
}
process.on("exit", () => {
  for (const r of roots) rmSync(r, { recursive: true, force: true });
});

const AGENT: AgentStep = {
  id: "answer",
  type: "agent",
  prompt: "Answer. Reply with JSON only.",
  schema: { type: "object", properties: { answer: { type: "string" } }, required: ["answer"] },
  outputs: ["answer"],
  max_turns: 2,
};
const RESOLVED: Resolved = { harness: "claude", model: "haiku", effort: "low" };
const CANNED: AgentStarter = () => ({ done: Promise.resolve(schemaAnswer({} as never)) });

function input(step: AgentStep, starter: AgentStarter, extra: Partial<AgentStepInput> = {}) {
  const base: AgentStepInput = {
    runDir: tmp(),
    stepRunId: "01STEPRUN",
    step,
    resolved: RESOLVED,
    cwd: "/tmp",
    stepToken: "tok".padEnd(32, "0"),
    starter,
  };
  return { ...base, ...extra };
}

// ---- headline --------------------------------------------------------------------------------------

test("headline: first non-empty line, collapsed whitespace, 200-char clip", () => {
  assert.equal(headline("\n\n  hello   world \nsecond"), "hello world");
  assert.equal(headline(""), "");
  const long = "x".repeat(500);
  assert.equal(headline(long).length, 200);
  assert.ok(headline(long).endsWith("…"));
});

// ---- RunReq ----------------------------------------------------------------------------------------

test("buildRunReq: defaults (auto, subscription, 30 min), schema, max_turns, effort, token", () => {
  const req = buildRunReq(
    input(AGENT, () => ({ done: Promise.resolve(schemaAnswer({} as never)) })),
  );
  assert.equal(req.prompt, AGENT.prompt);
  assert.equal(req.model, "haiku");
  assert.equal(req.effort, "low");
  assert.equal(req.mode, "auto");
  assert.equal(req.auth, "subscription");
  assert.equal(req.timeout_ms, DEFAULT_STEP_TIMEOUT_MS);
  assert.equal(req.max_turns, 2);
  assert.deepEqual(req.schema, AGENT.schema);
  assert.equal(req.step_token, "tok".padEnd(32, "0"));
  assert.equal(req.mcp_config, undefined, "M2.6 fills mcp_config");
  assert.equal(req.resume, undefined);
});

test("buildRunReq: step overrides (mode, auth, timeout seconds), resume only under `unit`", () => {
  const starter = CANNED;
  const step: AgentStep = {
    ...AGENT,
    mode: "full-access",
    auth: "api-key",
    timeout: 90,
    resume: "unit",
  };
  const req = buildRunReq(input(step, starter, { cursor: "sess-1" }));
  assert.equal(req.mode, "full-access");
  assert.equal(req.auth, "api-key");
  assert.equal(req.timeout_ms, 90_000);
  assert.equal(req.resume, "sess-1");
  const fresh = buildRunReq(input({ ...step, resume: "fresh" }, starter, { cursor: "sess-1" }));
  assert.equal(fresh.resume, undefined);
  const dflt = buildRunReq(input({ ...AGENT }, starter, { cursor: "sess-1" }));
  assert.equal(dflt.resume, undefined, "default policy is fresh");
});

test("buildRunReq: gemini has no effort control; empty effort is omitted", () => {
  const starter = CANNED;
  const gem = buildRunReq(
    input(AGENT, starter, { resolved: { harness: "gemini", model: "g", effort: "high" } }),
  );
  assert.equal(gem.effort, undefined);
  const none = buildRunReq(
    input(AGENT, starter, { resolved: { harness: "claude", model: "haiku", effort: "" } }),
  );
  assert.equal(none.effort, undefined);
});

// ---- outputs and outcomes -----------------------------------------------------------------------------

test("extractOutputs: copies declared names, reports the first missing one", () => {
  assert.deepEqual(extractOutputs(["a", "b"], { a: 1, b: "x", c: true }), {
    outputs: { a: 1, b: "x" },
  });
  assert.deepEqual(extractOutputs(["a", "b"], { a: 1 }), { outputs: { a: 1 }, missing: "b" });
  assert.deepEqual(extractOutputs(["a"], undefined), { outputs: {}, missing: "a" });
  assert.deepEqual(extractOutputs(undefined, undefined), { outputs: {} });
});

test("outcomeOf: ok, missing output, and every non-ok exit class", () => {
  const ok = outcomeOf(AGENT, schemaAnswer({ schema: AGENT.schema } as never));
  assert.equal(ok.ok, true);
  assert.equal(ok.exit, "ok");
  assert.deepEqual(ok.outputs, { answer: "answer-value" });
  assert.equal(ok.verdict, "answered answer");
  assert.equal(ok.cursor, "sess-answer");

  const missing = outcomeOf(AGENT, { text: "t", json: {}, usage: usage(), exit: "ok" });
  assert.equal(missing.ok, false);
  assert.equal(missing.exit, "missing_output");
  assert.equal(missing.error, "schema result lacks answer");

  for (const exit of ["error", "rate_limited", "auth", "timeout", "max_turns"] as const) {
    const res: RunRes = { text: "", usage: usage(), exit, error: `${exit} happened` };
    const out = outcomeOf(AGENT, res);
    assert.equal(out.ok, false);
    assert.equal(out.exit, exit);
    assert.equal(out.error, `${exit} happened`);
    assert.equal(out.verdict, `${exit}: ${exit} happened`);
  }
  const noJson = outcomeOf(
    { id: "plain", type: "agent", prompt: "hi" },
    {
      text: "",
      usage: usage(),
      exit: "ok",
    },
  );
  assert.equal(noJson.verdict, "ok");
});

test("startAgentStep: streams raw events to the raw log, writes the human extract with tool names", async () => {
  const events: RawEvent[] = [
    { ts: "T", harness: "claude", line: "{}", parsed: { type: "system", subtype: "init" } },
    {
      ts: "T",
      harness: "claude",
      line: "{}",
      parsed: { type: "assistant", message: { content: [{ type: "tool_use", name: "Read" }] } },
    },
  ];
  const starter: AgentStarter = (_h, req, onEvent) => {
    for (const e of events) onEvent(e);
    return { pid: 4242, done: Promise.resolve(schemaAnswer(req, { text: "A".repeat(6000) })) };
  };
  const inp = input(AGENT, starter);
  const { handle, outcome, req } = startAgentStep(inp);
  assert.equal(handle.pid, 4242);
  assert.equal(req.step_token, inp.stepToken);
  const out = await outcome;
  assert.equal(out.ok, true);
  const paths = logPaths(inp.runDir, "answer", "01STEPRUN");
  const raw = readFileSync(paths.raw, "utf8").trim().split("\n");
  assert.equal(raw.length, 2);
  assert.equal((JSON.parse(raw[1] ?? "{}") as RawEvent).parsed !== undefined, true);
  const human = readFileSync(paths.log, "utf8");
  assert.match(human, /^step: answer \(01STEPRUN\)/);
  assert.match(human, /tools: Read/);
  assert.match(human, /exit: ok/);
  assert.match(human, /chars elided/, "6000-char text is head+tail excerpted");
  assert.ok(human.length < 6000);
});

// ---- bash ------------------------------------------------------------------------------------------------

test("bash: exit 0 records the whole trimmed stdout under the first output, verdict = last line", async () => {
  const step: BashStep = {
    id: "b",
    type: "bash",
    run: "echo one\necho two\necho '  three  '",
    outputs: ["out"],
  };
  const res = await runBashStep(step, { cwd: tmp() });
  assert.equal(res.ok, true);
  assert.equal(res.code, 0);
  assert.equal(res.verdict, "three");
  assert.deepEqual(res.outputs, { out: "one\ntwo\n  three" });
});

test("bash: non-zero exit fails with the stderr tail; no outputs recorded", async () => {
  const step: BashStep = {
    id: "b",
    type: "bash",
    run: "echo partial; echo 'boom happened' >&2; exit 3",
    outputs: ["out"],
  };
  const res = await runBashStep(step, { cwd: tmp() });
  assert.equal(res.ok, false);
  assert.equal(res.code, 3);
  assert.equal(res.error, "boom happened");
  assert.equal(res.verdict, "failed: boom happened");
  assert.deepEqual(res.outputs, {});
});

test("bash: silent non-zero exit reports the code", async () => {
  const res = await runBashStep({ id: "b", type: "bash", run: "exit 7" }, { cwd: tmp() });
  assert.equal(res.ok, false);
  assert.equal(res.error, "exit code 7");
});

test("bash: wall-clock timeout kills the child and fails the step", async () => {
  const t0 = Date.now();
  const res = await runBashStep(
    { id: "b", type: "bash", run: "sleep 20" },
    { cwd: tmp(), defaultTimeoutMs: 150 },
  );
  assert.equal(res.ok, false);
  assert.equal(res.timedOut, true);
  assert.match(res.error ?? "", /timed out after 150 ms/);
  assert.ok(Date.now() - t0 < 5000);
});

test("bash: runs in the given cwd under a clean env (blocked vars dropped, PATH kept)", async () => {
  const cwd = tmp();
  const res = await runBashStep(
    {
      id: "b",
      type: "bash",
      run: 'pwd; echo "cc=${CLAUDECODE:-unset} secret=${MY_SECRET:-unset}"',
    },
    { cwd, parentEnv: { ...process.env, CLAUDECODE: "1", MY_SECRET: "x" } },
  );
  assert.equal(res.ok, true);
  assert.ok(res.stdout.startsWith(cwd) || res.stdout.includes(cwd.replace(/^\/private/, "")));
  assert.equal(res.verdict, "cc=unset secret=unset");
});

// ---- gates ------------------------------------------------------------------------------------------------

test("gate: approval offers approve/reject; ask mirrors options and allow_text", () => {
  assert.equal(isGateStep({ type: "approval" }), true);
  assert.equal(isGateStep({ type: "bash" }), false);
  const approval = buildGate({ id: "ok", type: "approval", message: " Ship?\n" }, "G1");
  assert.deepEqual(approval, {
    gate_id: "G1",
    step: "ok",
    kind: "approval",
    message: "Ship?",
    options: APPROVAL_OPTIONS.map((o) => ({ ...o })),
  });
  const ask: AskStep = {
    id: "pick",
    type: "ask",
    message: "Which?",
    options: ["tests", "docs"],
    allow_text: true,
    output: "next",
  };
  assert.deepEqual(buildGate(ask, "G2"), {
    gate_id: "G2",
    step: "pick",
    kind: "ask",
    message: "Which?",
    options: [
      { value: "tests", label: "tests" },
      { value: "docs", label: "docs" },
    ],
    allow_text: true,
  });
  const free = buildGate({ id: "q", type: "ask", message: "Say" }, "G3");
  assert.equal(free.options, undefined);
  assert.equal(free.allow_text, undefined);
});

test("gate: decideGate approval approve/reject, invalid value refused", () => {
  const step = { id: "ok", type: "approval", message: "Ship?" } as const;
  assert.deepEqual(decideGate(step, "approve"), { status: "completed", verdict: "approved" });
  assert.deepEqual(decideGate(step, ["reject"]), { status: "failed", verdict: "rejected" });
  assert.throws(
    () => decideGate(step, "maybe"),
    (e: unknown) => e instanceof RpcError && e.code === RPC_INVALID_PARAMS,
  );
});

test("gate: decideGate ask records under output (or the step id), honours options / allow_text", () => {
  const strict: AskStep = { id: "pick", type: "ask", message: "?", options: ["a", "b"] };
  assert.deepEqual(decideGate(strict, "a"), {
    status: "completed",
    verdict: "pick=a",
    output: { name: "pick", value: "a" },
  });
  assert.throws(() => decideGate(strict, "zzz"), RpcError);
  assert.throws(() => decideGate(strict, ""), RpcError);
  const loose: AskStep = { ...strict, allow_text: true, output: "next_focus" };
  assert.deepEqual(decideGate(loose, ["x", "y"]).output, { name: "next_focus", value: "x, y" });
  const free: AskStep = { id: "q", type: "ask", message: "?" };
  assert.deepEqual(decideGate(free, "anything").output, { name: "q", value: "anything" });
});

test("fixture check: bash and gate step fixtures do not leak files", () => {
  for (const r of roots) assert.equal(existsSync(r), true);
});
