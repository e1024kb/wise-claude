import { after, before, describe, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import type { CallToolResult, Progress } from "@modelcontextprotocol/sdk/types.js";
import { daemonPaths, startDaemon } from "../src/daemon.ts";
import type { Daemon, DaemonHandlers } from "../src/daemon.ts";
import { createMcpServer, MCP_TOOL_NAMES } from "../src/mcp.ts";
import type { McpServerOptions } from "../src/mcp.ts";
import { WAIT_DEFAULT_MS, WAIT_MAX_MS } from "../src/protocol.ts";
import type { ProgressParams, WaitResult } from "../src/protocol.ts";
import { domainError } from "../src/rpc.ts";
import type { RunSummary } from "../src/types.ts";
import { pluginVersion } from "../src/version.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const ENGINE = join(HERE, "..");
const PLUGIN = join(ENGINE, "..");
const VERSION = "9.9.9-mcp-test";

type Root = { root: string; env: Record<string, string> };
type Call = { method: string; params: unknown };

const roots: string[] = [];
const daemons = new Set<Daemon>();
const clients = new Set<Client>();

/** Fresh data root; the socket lands at `<tmp>/wm-XXXX/wise/engined.sock`. */
function mkRoot(): Root {
  const root = mkdtempSync(join(tmpdir(), "wm-"));
  roots.push(root);
  const env = { XDG_DATA_HOME: root, HOME: root };
  assert.ok(daemonPaths({ env }).socketPath.length < 100);
  return { root, env };
}

const SUMMARY: RunSummary = {
  run_id: "01RUN",
  workflow: "ticket-plan",
  status: "running",
  started_at: "2026-09-05T00:00:00Z",
  last_activity_at: "2026-09-05T00:00:01Z",
  cwd: "/work",
};

/** Canned handlers that record every call; `wait` emits two progress notifications first. */
function fakeHandlers(calls: Call[]): Partial<DaemonHandlers> {
  const record = (method: string, params: unknown): void => {
    calls.push({ method, params: structuredClone(params) });
  };
  return {
    preflight: (params) => {
      record("preflight", params);
      return {
        workflow: params.workflow,
        version: 2,
        questions: [{ id: "profile", kind: "choice", label: "Profile" }],
        defaults: { profile: "medium" },
      };
    },
    run: (params) => {
      record("run", params);
      if (params.workflow === "needs-auth") {
        throw domainError("AUTH_REQUIRED", "codex is not logged in", {
          harness: "codex",
          login_cmd: "codex login --device-auth",
        });
      }
      return { run_id: "01RUN", status: "running" };
    },
    wait: (params, ctx) => {
      record("wait", params);
      for (const waiting_ms of [30_000, 60_000]) {
        const p: ProgressParams = { run_id: params.run_id, waiting_ms };
        ctx.notify("progress", p);
      }
      const result: WaitResult = {
        events: [
          {
            seq: (params.after ?? 0) + 1,
            ts: "2026-09-05T00:00:02Z",
            run_id: params.run_id,
            type: "step.done",
            step: "plan",
            verdict: "planned 3 tickets",
          },
        ],
        status: "running",
        done: false,
      };
      return result;
    },
    answer: (params) => {
      record("answer", params);
      throw domainError("GATE_STALE", `gate ${params.gate_id} is closed`, {
        gate_id: params.gate_id,
      });
    },
    status: (params) => {
      record("status", params);
      return params.run_id === undefined ? [SUMMARY] : { ...SUMMARY, run_id: params.run_id };
    },
    cancel: (params) => {
      record("cancel", params);
      return { status: "cancelled" };
    },
  };
}

async function startFake(r: Root, calls: Call[], version = VERSION): Promise<Daemon> {
  const d = await startDaemon({
    env: r.env,
    version,
    idleMs: 60_000,
    handlers: fakeHandlers(calls),
  });
  daemons.add(d);
  return d;
}

/** MCP client wired to a fresh `createMcpServer` over an in-memory pair. */
async function openMcp(opts: McpServerOptions): Promise<Client> {
  const server = createMcpServer(opts);
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await server.connect(serverSide);
  const client = new Client({ name: "mcp-test", version: "0.0.0" });
  await client.connect(clientSide);
  clients.add(client);
  return client;
}

function textOf(res: CallToolResult): string {
  assert.equal(res.content.length, 1);
  const block = res.content[0];
  assert.ok(block && block.type === "text");
  return block.text;
}

function parsed(res: CallToolResult): unknown {
  return JSON.parse(textOf(res)) as unknown;
}

function errorOf(res: CallToolResult): Record<string, unknown> {
  assert.equal(res.isError, true);
  const body = parsed(res) as { error: Record<string, unknown> };
  return body.error;
}

async function callTool(
  client: Client,
  name: string,
  args: Record<string, unknown>,
  onprogress?: (p: Progress) => void,
): Promise<CallToolResult> {
  const res = await client.callTool(
    { name, arguments: args },
    undefined,
    onprogress ? { onprogress } : undefined,
  );
  return res as CallToolResult;
}

describe("mcp", () => {
  after(async () => {
    for (const c of clients) await c.close().catch(() => {});
    for (const d of daemons) await d.close();
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  describe("against a fake daemon", () => {
    const calls: Call[] = [];
    let r: Root;
    let client: Client;

    before(async () => {
      r = mkRoot();
      await startFake(r, calls);
      client = await openMcp({ daemon: { env: r.env, version: VERSION }, version: VERSION });
    });

    test("listTools exposes exactly the six P1 tools with input schemas", async () => {
      const { tools } = await client.listTools();
      assert.deepEqual(tools.map((t) => t.name).toSorted(), [...MCP_TOOL_NAMES].toSorted());
      for (const t of tools) {
        assert.ok(t.description && t.description.length > 20, t.name);
        assert.equal(t.inputSchema.type, "object");
        assert.ok(t.inputSchema.properties, t.name);
      }
      const wait = tools.find((t) => t.name === "wise_wait");
      assert.ok(wait);
      assert.deepEqual(Object.keys(wait.inputSchema.properties ?? {}).toSorted(), [
        "after",
        "run_id",
        "timeout_ms",
      ]);
      assert.match(wait.description ?? "", /never appear/);
    });

    test("wise_preflight forwards params and returns the result as compact JSON", async () => {
      calls.length = 0;
      const res = await callTool(client, "wise_preflight", { workflow: "ticket-plan", cwd: "/w" });
      assert.notEqual(res.isError, true);
      assert.deepEqual(calls, [
        { method: "preflight", params: { workflow: "ticket-plan", cwd: "/w" } },
      ]);
      const expected = {
        workflow: "ticket-plan",
        version: 2,
        questions: [{ id: "profile", kind: "choice", label: "Profile" }],
        defaults: { profile: "medium" },
      };
      assert.equal(textOf(res), JSON.stringify(expected));
      assert.deepEqual(res.structuredContent, expected);
    });

    test("wise_run forwards workflow, cwd, answers, context and inputs verbatim", async () => {
      calls.length = 0;
      const args = {
        workflow: "ticket-plan",
        cwd: "/w",
        answers: { profile: "low", "step-select": ["plan", "review"] },
        context: {
          ticket: [{ ref: "LEC-1", title: "T", body: "B", url: "https://x/LEC-1" }],
          guidance: "keep it small",
          decisions: { base: "main" },
          links: ["https://doc"],
        },
        inputs: { ticket_ids: "LEC-1" },
      };
      const res = await callTool(client, "wise_run", args);
      assert.deepEqual(calls, [{ method: "run", params: args }]);
      assert.equal(textOf(res), JSON.stringify({ run_id: "01RUN", status: "running" }));
    });

    test("wise_run defaults answers, context and inputs to empty objects", async () => {
      calls.length = 0;
      await callTool(client, "wise_run", { workflow: "ticket-plan", cwd: "/w" });
      assert.deepEqual(calls[0]?.params, {
        workflow: "ticket-plan",
        cwd: "/w",
        answers: {},
        context: {},
        inputs: {},
      });
    });

    test("AUTH_REQUIRED comes back as a tool error carrying login_cmd verbatim", async () => {
      const res = await callTool(client, "wise_run", { workflow: "needs-auth", cwd: "/w" });
      const err = errorOf(res);
      assert.equal(err.code, "AUTH_REQUIRED");
      assert.equal(err.message, "codex is not logged in");
      assert.equal(err.harness, "codex");
      assert.equal(err.login_cmd, "codex login --device-auth");
      assert.equal(textOf(res).startsWith('{"error":{"code":"AUTH_REQUIRED"'), true);
    });

    test("wise_wait defaults timeout_ms and forwards progress notifications", async () => {
      calls.length = 0;
      const progress: Progress[] = [];
      const res = await callTool(client, "wise_wait", { run_id: "01RUN" }, (p) => progress.push(p));
      assert.deepEqual(calls, [
        { method: "wait", params: { run_id: "01RUN", timeout_ms: WAIT_DEFAULT_MS } },
      ]);
      const body = parsed(res) as WaitResult;
      assert.equal(body.done, false);
      assert.equal(body.status, "running");
      assert.equal(body.events[0]?.seq, 1);
      assert.equal(body.events[0]?.verdict, "planned 3 tickets");
      assert.deepEqual(
        progress.map((p) => p.progress),
        [30_000, 60_000],
      );
      assert.match(progress[0]?.message ?? "", /01RUN.*30 s/);
      assert.deepEqual(res.structuredContent, body);
    });

    test("wise_wait clamps timeout_ms to the cap and forwards after", async () => {
      calls.length = 0;
      const res = await callTool(client, "wise_wait", {
        run_id: "01RUN",
        after: 7,
        timeout_ms: WAIT_MAX_MS + 1,
      });
      assert.deepEqual(calls, [
        { method: "wait", params: { run_id: "01RUN", after: 7, timeout_ms: WAIT_MAX_MS } },
      ]);
      assert.equal((parsed(res) as WaitResult).events[0]?.seq, 8);
    });

    test("wise_wait without a progress token still returns the result", async () => {
      calls.length = 0;
      const res = await callTool(client, "wise_wait", { run_id: "01RUN", timeout_ms: 5 });
      assert.equal(calls[0]?.params && (calls[0].params as { timeout_ms: number }).timeout_ms, 5);
      assert.equal((parsed(res) as WaitResult).status, "running");
    });

    test("wise_answer maps a domain error to isError with its code", async () => {
      calls.length = 0;
      const res = await callTool(client, "wise_answer", {
        run_id: "01RUN",
        gate_id: "g1",
        value: ["a", "b"],
      });
      assert.deepEqual(calls, [
        { method: "answer", params: { run_id: "01RUN", gate_id: "g1", value: ["a", "b"] } },
      ]);
      const err = errorOf(res);
      assert.equal(err.code, "GATE_STALE");
      assert.equal(err.gate_id, "g1");
      assert.equal(err.message, "gate g1 is closed");
    });

    test("wise_status: array result is JSON text without structuredContent, object result has it", async () => {
      calls.length = 0;
      const all = await callTool(client, "wise_status", {});
      assert.deepEqual(calls, [{ method: "status", params: {} }]);
      assert.equal(textOf(all), JSON.stringify([SUMMARY]));
      assert.equal(all.structuredContent, undefined);

      calls.length = 0;
      const one = await callTool(client, "wise_status", { run_id: "01X" });
      assert.deepEqual(calls, [{ method: "status", params: { run_id: "01X" } }]);
      assert.equal((one.structuredContent as RunSummary).run_id, "01X");
    });

    test("wise_cancel forwards run_id and reason", async () => {
      calls.length = 0;
      const res = await callTool(client, "wise_cancel", { run_id: "01RUN", reason: "operator" });
      assert.deepEqual(calls, [
        { method: "cancel", params: { run_id: "01RUN", reason: "operator" } },
      ]);
      assert.equal(textOf(res), '{"status":"cancelled"}');
    });

    test("invalid arguments are rejected by the input schema before reaching the daemon", async () => {
      calls.length = 0;
      const res = await callTool(client, "wise_cancel", {});
      assert.equal(res.isError, true);
      assert.match(textOf(res), /run_id/);
      assert.deepEqual(calls, []);
    });
  });

  test("a dropped socket reconnects once: daemon restarted between two calls", async () => {
    const r = mkRoot();
    const first: Call[] = [];
    const d1 = await startFake(r, first);
    const client = await openMcp({ daemon: { env: r.env, version: VERSION }, version: VERSION });
    await callTool(client, "wise_status", {});
    assert.equal(first.length, 1);

    await d1.close();
    daemons.delete(d1);
    const second: Call[] = [];
    await startFake(r, second);
    const res = await callTool(client, "wise_status", { run_id: "01B" });
    assert.notEqual(res.isError, true, textOf(res));
    assert.equal(second.length, 1);
    assert.equal(first.length, 1);
  });

  test("dead daemon with auto-start disabled yields DAEMON_UNAVAILABLE with the init hint", async () => {
    const r = mkRoot();
    const client = await openMcp({
      daemon: { env: r.env, version: VERSION },
      version: VERSION,
      autoStart: false,
    });
    const res = await callTool(client, "wise_status", {});
    const err = errorOf(res);
    assert.equal(err.code, "DAEMON_UNAVAILABLE");
    assert.equal(err.cause, "DAEMON_UNAVAILABLE");
    assert.match(String(err.hint), /\/wise-init/);
  });

  test("stdio smoke: the real entry point serves wise_status on the current runtime", async () => {
    const r = mkRoot();
    const calls: Call[] = [];
    await startFake(r, calls, pluginVersion());
    // Prefer `engine.sh mcp` once the CLI dispatches it; until then run src/mcp.ts directly.
    const cli = readFileSync(join(ENGINE, "src", "cli.ts"), "utf8");
    const viaCli = /mcpCommand/.test(cli);
    const env: Record<string, string> = {};
    for (const [k, v] of Object.entries(process.env)) if (v !== undefined) env[k] = v;
    delete env.XDG_RUNTIME_DIR;
    Object.assign(env, r.env);
    const transport = new StdioClientTransport({
      command: viaCli ? "bash" : process.execPath,
      args: viaCli
        ? [join(ENGINE, "engine.sh"), "mcp", "--no-start"]
        : [join(ENGINE, "src", "mcp.ts"), "--no-start"],
      env,
      stderr: "inherit",
    });
    const client = new Client({ name: "stdio-smoke", version: "0.0.0" });
    clients.add(client);
    await client.connect(transport);
    const info = client.getServerVersion();
    assert.equal(info?.name, "wise-engine");
    assert.equal(info?.version, pluginVersion());
    const { tools } = await client.listTools();
    assert.equal(tools.length, 6);
    const res = await callTool(client, "wise_status", { run_id: "01S" });
    assert.notEqual(res.isError, true, textOf(res));
    assert.equal((parsed(res) as RunSummary).run_id, "01S");
    assert.deepEqual(calls, [{ method: "status", params: { run_id: "01S" } }]);
    await client.close();
    clients.delete(client);
  });

  test("plugins/wise/.mcp.json declares the wise-engine server", () => {
    const raw = readFileSync(join(PLUGIN, ".mcp.json"), "utf8");
    const json = JSON.parse(raw) as {
      mcpServers: Record<string, { command: string; args: string[]; timeout: number }>;
    };
    const server = json.mcpServers["wise-engine"];
    assert.ok(server);
    assert.equal(server.command, "bash");
    assert.deepEqual(server.args, ["${CLAUDE_PLUGIN_ROOT}/engine/engine.sh", "mcp"]);
    assert.equal(server.timeout, 660_000);
    assert.ok(server.timeout > WAIT_MAX_MS);
  });
});
