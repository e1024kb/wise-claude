import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { connect } from "../src/client.ts";
import type { Client as DaemonClient } from "../src/client.ts";
import { daemonPaths, startDaemon } from "../src/daemon.ts";
import type { Daemon } from "../src/daemon.ts";
import { executorHandlers } from "../src/executor.ts";
import type { Executor } from "../src/executor.ts";
import { readEvents, readState } from "../src/ledger.ts";
import type { WaitResult } from "../src/protocol.ts";
import type { Event, RunSummary } from "../src/types.ts";
import { createUnitMcpServer, UNIT_MCP_TOOL_NAMES } from "../src/unit-mcp.ts";
import { fakeAdapter, pause, schemaAnswer } from "./fixtures/executor/fake.ts";
import { heldStarter } from "./fixtures/executor/held.ts";
import type { Held } from "./fixtures/executor/held.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFS = join(HERE, "..", "..", "workflows");
const EXEC_FIXTURES = join(HERE, "fixtures", "executor");
const VERSION = "9.9.9-unit-mcp-test";

type Root = { root: string; env: Record<string, string>; cwd: string };

const roots: string[] = [];
const daemons = new Set<Daemon>();
const executors = new Set<Executor>();
const clients = new Set<Client>();
const daemonClients = new Set<DaemonClient>();

function mkRoot(): Root {
  const root = mkdtempSync(join(tmpdir(), "wu-"));
  roots.push(root);
  const env = { XDG_DATA_HOME: root, HOME: root, PATH: process.env.PATH ?? "" };
  assert.ok(daemonPaths({ env }).socketPath.length < 100);
  const cwd = join(root, "proj");
  mkdirSync(cwd);
  return { root, env, cwd };
}

async function startEngine(r: Root): Promise<{
  daemon: Daemon;
  executor: Executor;
  held: Held[];
  client: DaemonClient;
}> {
  const { starter, held } = heldStarter();
  let executor: Executor | undefined;
  const daemon = await startDaemon({
    env: r.env,
    version: VERSION,
    idleMs: 60_000,
    handlers: executorHandlers(
      {
        env: r.env,
        roots: { userRoot: EXEC_FIXTURES, bundledRoot: DEFS },
        configPath: join(r.root, "missing.json"),
        adapters: { claude: fakeAdapter("claude", (req) => schemaAnswer(req)) },
        startAgent: starter,
        channel: { engineRoot: "/opt/wise/engine" },
      },
      (e) => {
        executor = e;
      },
    ),
  });
  daemons.add(daemon);
  assert.ok(executor);
  executors.add(executor);
  const client = await connect({ env: r.env, version: VERSION, client: "test", timeoutMs: 10_000 });
  daemonClients.add(client);
  return { daemon, executor, held, client };
}

async function openUnit(r: Root, token: string, askTimeoutMs?: number): Promise<Client> {
  const server = createUnitMcpServer({
    token,
    daemon: { env: r.env, version: VERSION },
    version: VERSION,
    ...(askTimeoutMs !== undefined ? { askTimeoutMs } : {}),
  });
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await server.connect(serverSide);
  const client = new Client({ name: "unit-mcp-test", version: "0.0.0" });
  await client.connect(clientSide);
  clients.add(client);
  return client;
}

async function call(
  client: Client,
  name: string,
  args: Record<string, unknown>,
): Promise<CallToolResult> {
  return (await client.callTool({ name, arguments: args })) as CallToolResult;
}

function textOf(res: CallToolResult): string {
  const block = res.content[0];
  assert.ok(block && block.type === "text");
  return block.text;
}

function parsed(res: CallToolResult): unknown {
  return JSON.parse(textOf(res)) as unknown;
}

async function until(pred: () => boolean, what: string, timeoutMs = 15_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (pred()) return;
    await pause(10);
  }
  assert.fail(`timed out waiting for ${what}`);
}

describe("unit-mcp", () => {
  after(async () => {
    for (const c of clients) await c.close().catch(() => {});
    for (const c of daemonClients) c.close();
    for (const e of executors) e.stop();
    for (const d of daemons) await d.close();
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  describe("interactive run with a live child", () => {
    let r: Root;
    let executor: Executor;
    let held: Held[];
    let client: DaemonClient;
    let unit: Client;
    let runId: string;
    let runDir: string;
    let token: string;

    before(async () => {
      r = mkRoot();
      const engine = await startEngine(r);
      executor = engine.executor;
      held = engine.held;
      client = engine.client;
      const res = await client.call("run", {
        workflow: "channel",
        cwd: r.cwd,
        answers: {},
        context: {
          guidance: "keep it small",
          ticket: [{ ref: "LEC-1", title: "T", body: "the ticket body" }],
          decisions: {},
        },
        inputs: {},
      });
      runId = res.run_id;
      runDir = engine.daemon.runtime.requireRunDir(runId);
      await until(() => held.length === 1, "the agent child to start");
      token = executor.stepToken(runId, "work") ?? "";
      assert.equal(token.length, 32);
      unit = await openUnit(r, token, 50);
    });

    test("the child gets exactly one MCP server with the token in env, never in argv", () => {
      const req = held[0]?.req;
      assert.ok(req);
      const paths = daemonPaths({ env: r.env });
      assert.deepEqual(req.mcp_config, {
        mcpServers: {
          "wise-engine": {
            command: "bash",
            args: ["/opt/wise/engine/engine.sh", "unit-mcp"],
            env: {
              WISE_STEP_TOKEN: token,
              WISE_ENGINE_SOCKET: paths.socketPath,
              WISE_DATA_ROOT: paths.dataRoot,
            },
          },
        },
      });
      assert.equal(req.step_token, token);
    });

    test("listTools exposes the four child tools with descriptions", async () => {
      const { tools } = await unit.listTools();
      assert.deepEqual(tools.map((t) => t.name).toSorted(), [...UNIT_MCP_TOOL_NAMES].toSorted());
      for (const t of tools) assert.ok((t.description ?? "").length > 20, t.name);
    });

    test("wise_report appends a compact step.progress event; text clipped, big data dropped", async () => {
      const res = await call(unit, "wise_report", {
        kind: "finding",
        text: "found the bug\nin file x",
        data: { file: "src/x.ts" },
      });
      assert.notEqual(res.isError, true, textOf(res));
      const body = parsed(res) as { accepted: boolean; seq: number };
      assert.equal(body.accepted, true);
      const ev = readEvents(runDir).find((e) => e.seq === body.seq);
      assert.ok(ev);
      assert.equal(ev.type, "step.progress");
      assert.equal(ev.step, "work");
      assert.equal(ev.kind, "finding");
      assert.equal(ev.message, "found the bug");
      assert.deepEqual(ev.data, { file: "src/x.ts" });

      const big = await call(unit, "wise_report", {
        kind: "progress",
        text: "y".repeat(500),
        data: { blob: "z".repeat(2000) },
      });
      const bigBody = parsed(big) as { seq: number };
      const bigEv = readEvents(runDir).find((e) => e.seq === bigBody.seq) as Event;
      assert.equal(bigEv.message?.length, 200);
      assert.equal(bigEv.data, undefined);

      const bad = await call(unit, "wise_report", { kind: "shout", text: "x" });
      assert.equal(bad.isError, true);
    });

    test("wise_context resolves inputs, prior outputs (by name and step), context keys, else null", async () => {
      const get = async (key: string): Promise<unknown> =>
        (parsed(await call(unit, "wise_context", { key })) as { value: unknown }).value;
      assert.equal(await get("topic"), "engines");
      assert.equal(await get("prep_out"), "prepared");
      assert.deepEqual(await get("prep"), { prep_out: "prepared" });
      assert.equal(await get("guidance"), "keep it small");
      // The body went to a file at run creation; children get the path and read it.
      assert.equal(await get("ticket.0.body"), null);
      const ticketPath = join(runDir, "context", "tickets", "LEC-1.md");
      assert.deepEqual(await get("ticket"), [{ ref: "LEC-1", title: "T", path: ticketPath }]);
      assert.match(
        readFileSync(ticketPath, "utf8"),
        /^> Tracker text[^\n]*\n\n---\nref: "LEC-1"\ntitle: "T"\n[\s\S]*# LEC-1: T\n\nthe ticket body\n$/,
      );
      assert.equal(await get("nope"), null);
    });

    test("wise_checkpoint writes checkpoints/<step>.json atomically", async () => {
      const res = await call(unit, "wise_checkpoint", { data: { done: ["a", "b"], next: "c" } });
      const body = parsed(res) as { path: string };
      assert.equal(body.path, join(runDir, "checkpoints", "work.json"));
      assert.deepEqual(JSON.parse(readFileSync(body.path, "utf8")), {
        done: ["a", "b"],
        next: "c",
      });
      assert.equal(existsSync(body.path + ".tmp"), false);
    });

    test("wise_ask opens an ask gate on the running step; answer resolves it and nudges the child", async () => {
      const lastSeq = readEvents(runDir).at(-1)?.seq ?? 0;
      const pendingAsk = call(unit, "wise_ask", {
        question: "Which area next?",
        options: ["tests", "docs"],
      });
      const waited = (await client.call("wait", {
        run_id: runId,
        after: lastSeq,
        timeout_ms: 5000,
      })) as WaitResult;
      assert.equal(waited.status, "gated");
      assert.ok(waited.gate);
      assert.equal(waited.gate.kind, "ask");
      assert.equal(waited.gate.step, "work");
      assert.equal(waited.gate.message, "Which area next?");
      assert.deepEqual(
        waited.gate.options?.map((o) => o.value),
        ["tests", "docs"],
      );
      assert.equal(readState(runDir).steps.work?.status, "running");
      assert.ok(waited.events.some((e) => e.type === "gate.opened" && e.step === "work"));

      // The 50 ms long-poll bound has passed several times by now: the tool re-issued with ask_id.
      await pause(150);
      const rejected = await client
        .call("answer", { run_id: runId, gate_id: waited.gate.gate_id, value: "zzz" })
        .catch((e: unknown) => e);
      assert.ok(rejected instanceof Error, "an option outside the list is refused");

      const answered = await client.call("answer", {
        run_id: runId,
        gate_id: waited.gate.gate_id,
        value: "docs",
      });
      assert.deepEqual(answered, { accepted: true });
      const res = await pendingAsk;
      assert.notEqual(res.isError, true, textOf(res));
      assert.deepEqual(parsed(res), { value: "docs" });
      assert.deepEqual(held[0]?.nudges, ["Answer to your question: docs"]);
      const state = readState(runDir);
      assert.equal(state.status, "running");
      assert.equal(state.gate, undefined);
      assert.equal(state.outputs.work, undefined, "a child ask records no output");
      const events = readEvents(runDir);
      const gateAnswered = events.find((e) => e.type === "gate.answered" && e.step === "work");
      assert.equal(gateAnswered?.verdict, "answered: docs");
      assert.equal(
        events.some((e) => e.type === "step.done" && e.step === "work"),
        false,
      );
    });

    test("wise_status carries the live child under children", async () => {
      held[0]?.push({
        type: "assistant",
        message: { content: [{ type: "tool_use", name: "Edit" }], usage: { input_tokens: 40 } },
      });
      const summary = (await client.call("status", { run_id: runId })) as RunSummary;
      assert.equal(summary.children?.length, 1);
      const child = summary.children?.[0];
      assert.equal(child?.step, "work");
      assert.equal(child?.turn, 1);
      assert.equal(child?.tool, "Edit");
      assert.equal(child?.tokens, 40);
      assert.equal(child?.reports, 2);
      assert.match(child?.last_activity ?? "", /^\d{4}-/);
    });

    test("a bad token is TOKEN_INVALID; the token dies with the step", async () => {
      const bad = await openUnit(r, "deadbeef".repeat(4));
      const res = await call(bad, "wise_report", { kind: "progress", text: "hi" });
      assert.equal(res.isError, true);
      assert.equal((parsed(res) as { error: { code: string } }).error.code, "TOKEN_INVALID");

      const none = await openUnit(r, "");
      const noTok = await call(none, "wise_context", { key: "topic" });
      assert.equal((parsed(noTok) as { error: { code: string } }).error.code, "TOKEN_INVALID");

      held[0]?.finish(schemaAnswer(held[0].req));
      await until(() => readState(runDir).status === "completed", "run completion");
      assert.equal(readState(runDir).outputs.result, "result-value");
      assert.equal(executor.stepToken(runId, "work"), undefined);
      const stale = await call(unit, "wise_report", { kind: "progress", text: "late" });
      assert.equal((parsed(stale) as { error: { code: string } }).error.code, "TOKEN_INVALID");
    });
  });

  test("synchronous run: wise_ask answers from decisions, then the first option, else needs-human", async () => {
    const r = mkRoot();
    const { daemon, executor, held, client } = await startEngine(r);
    const { run_id } = await client.call("run", {
      workflow: "channel",
      cwd: r.cwd,
      answers: { "control-mode": "synchronous" },
      context: { decisions: { "Which base branch?": "release/2", risk: "low" } },
      inputs: {},
    });
    const runDir = daemon.runtime.requireRunDir(run_id);
    await until(() => held.length === 1, "the agent child to start");
    const unit = await openUnit(r, executor.stepToken(run_id, "work") ?? "");

    const exact = await call(unit, "wise_ask", { question: "Which base branch?" });
    assert.deepEqual(parsed(exact), { value: "release/2" });
    const partial = await call(unit, "wise_ask", {
      question: "What risk level do you accept?",
      options: ["high", "low"],
    });
    assert.deepEqual(parsed(partial), { value: "low" });
    const first = await call(unit, "wise_ask", { question: "Colour?", options: ["red", "blue"] });
    assert.deepEqual(parsed(first), { value: "red" });
    const human = await call(unit, "wise_ask", { question: "Anything else?" });
    assert.equal(human.isError, true);
    assert.equal((parsed(human) as { error: { error: string } }).error.error, "needs-human");

    const events = readEvents(runDir);
    assert.equal(
      events.some((e) => e.type === "gate.opened"),
      false,
    );
    assert.equal(
      events.filter((e) => e.type === "step.progress" && e.kind === "decision").length,
      3,
    );
    assert.ok(events.some((e) => e.type === "warn" && /no decision/.test(e.message ?? "")));
    assert.equal(held[0]?.nudges.length, 0);
    held[0]?.finish(schemaAnswer(held[0].req));
    await until(() => readState(runDir).status === "completed", "run completion");
  });

  test("a child that exits with an open question closes its gate and warns", async () => {
    const r = mkRoot();
    const { daemon, executor, held, client } = await startEngine(r);
    const { run_id } = await client.call("run", {
      workflow: "channel",
      cwd: r.cwd,
      answers: {},
      context: {},
      inputs: {},
    });
    const runDir = daemon.runtime.requireRunDir(run_id);
    await until(() => held.length === 1, "the agent child to start");
    const unit = await openUnit(r, executor.stepToken(run_id, "work") ?? "", 50);
    const asking = call(unit, "wise_ask", { question: "Stuck?" });
    await until(() => readState(runDir).status === "gated", "the ask gate");
    held[0]?.finish(schemaAnswer(held[0].req));
    await until(() => readState(runDir).status === "completed", "run completion");
    const res = await asking;
    assert.equal(res.isError, true);
    assert.equal((parsed(res) as { error: { code: string } }).error.code, "GATE_STALE");
    const events = readEvents(runDir);
    assert.ok(events.some((e) => e.type === "warn" && /open question/.test(e.message ?? "")));
    assert.equal(readState(runDir).gate, undefined);
  });
});
