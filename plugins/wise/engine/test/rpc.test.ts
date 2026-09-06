import { after, describe, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { createConnection, createServer } from "node:net";
import type { Server, Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  RPC_CLIENT_DISCONNECTED,
  RPC_CLIENT_TIMEOUT,
  RPC_DOMAIN_ERROR,
  RPC_INTERNAL_ERROR,
  RPC_INVALID_REQUEST,
  RPC_METHOD_NOT_FOUND,
  RPC_PARSE_ERROR,
} from "../src/protocol.ts";
import {
  domainCode,
  domainError,
  LineFramer,
  RpcClient,
  RpcError,
  serveConnection,
} from "../src/rpc.ts";
import type { CallContext, RpcHandlerMap, ServeOptions } from "../src/rpc.ts";

type Pair = {
  socketPath: string;
  server: Server;
  raw: () => Promise<Socket>;
  client: (timeoutMs?: number) => Promise<RpcClient>;
  close: () => Promise<void>;
};

const roots: string[] = [];
const pairs: Pair[] = [];

function shortTmp(): string {
  const dir = mkdtempSync(join(tmpdir(), "wr-"));
  roots.push(dir);
  return dir;
}

async function startPair(handlers: RpcHandlerMap, serve: ServeOptions = {}): Promise<Pair> {
  const socketPath = join(shortTmp(), "s.sock");
  const server = createServer((socket) => serveConnection(socket, handlers, serve));
  await new Promise<void>((resolve) => server.listen(socketPath, resolve));
  const sockets = new Set<Socket>();
  const raw = (): Promise<Socket> =>
    new Promise((resolve, reject) => {
      const s = createConnection(socketPath);
      sockets.add(s);
      s.once("connect", () => resolve(s));
      s.once("error", reject);
    });
  const pair: Pair = {
    socketPath,
    server,
    raw,
    client: async (timeoutMs = 2000) => new RpcClient(await raw(), { timeoutMs }),
    close: async () => {
      for (const s of sockets) s.destroy();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    },
  };
  pairs.push(pair);
  return pair;
}

/** Collect `n` newline-terminated lines from a raw socket. */
function readLines(socket: Socket, n: number, timeoutMs = 2000): Promise<unknown[]> {
  return new Promise((resolve, reject) => {
    const framer = new LineFramer();
    const out: unknown[] = [];
    const timer = setTimeout(() => reject(new Error(`only ${out.length}/${n} lines`)), timeoutMs);
    socket.on("data", (chunk: Buffer) => {
      for (const line of framer.push(chunk)) out.push(JSON.parse(line));
      if (out.length >= n) {
        clearTimeout(timer);
        resolve(out);
      }
    });
  });
}

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const echoHandlers: RpcHandlerMap = {
  echo: (params) => params,
  add: (params) => {
    const { a, b } = params as { a: number; b: number };
    return a + b;
  },
  fail_domain: () => {
    throw domainError("RUN_NOT_FOUND", "no run", { run_id: "x" });
  },
  fail_plain: () => {
    throw new Error("boom");
  },
  never: () => new Promise(() => {}),
  tick: (_params, ctx: CallContext) => {
    ctx.notify("progress", { n: 1 });
    ctx.notify("progress", { n: 2 });
    return "ticked";
  },
};

describe("rpc", () => {
  after(async () => {
    for (const p of pairs) await p.close();
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  test("LineFramer: partial chunks and several lines per chunk", () => {
    const f = new LineFramer();
    assert.deepEqual(f.push('{"a":'), []);
    assert.deepEqual(f.push('1}\n{"b":2}\n{"c"'), ['{"a":1}', '{"b":2}']);
    assert.deepEqual(f.push(":3}\r\n\n"), ['{"c":3}']);
  });

  test("server: a request split across chunks gets one reply", async () => {
    const pair = await startPair(echoHandlers);
    const s = await pair.raw();
    const wire = JSON.stringify({ jsonrpc: "2.0", id: 7, method: "add", params: { a: 2, b: 3 } });
    const replies = readLines(s, 1);
    s.write(wire.slice(0, 15));
    await pause(20);
    s.write(wire.slice(15) + "\n");
    assert.deepEqual(await replies, [{ jsonrpc: "2.0", id: 7, result: 5 }]);
  });

  test("server: two requests in one chunk get two replies", async () => {
    const pair = await startPair(echoHandlers);
    const s = await pair.raw();
    const replies = readLines(s, 2);
    s.write(
      JSON.stringify({ jsonrpc: "2.0", id: "a", method: "echo", params: 1 }) +
        "\n" +
        JSON.stringify({ jsonrpc: "2.0", id: "b", method: "echo", params: [2] }) +
        "\n",
    );
    const got = (await replies) as { id: string; result: unknown }[];
    got.sort((x, y) => (x.id < y.id ? -1 : 1));
    assert.deepEqual(got, [
      { jsonrpc: "2.0", id: "a", result: 1 },
      { jsonrpc: "2.0", id: "b", result: [2] },
    ]);
  });

  test("server: error responses", async () => {
    const pair = await startPair(echoHandlers);
    const s = await pair.raw();
    const replies = readLines(s, 5);
    s.write("this is not json\n");
    s.write('{"jsonrpc":"1.0","id":2}\n');
    s.write('{"jsonrpc":"2.0","id":3,"method":"nope"}\n');
    s.write('{"jsonrpc":"2.0","id":4,"method":"fail_domain"}\n');
    s.write('{"jsonrpc":"2.0","id":5,"method":"fail_plain"}\n');
    const got = (await replies) as { id: number | null; error: { code: number; data?: unknown } }[];
    const byId = new Map(got.map((r) => [r.id, r.error]));
    assert.equal(byId.get(null)?.code, RPC_PARSE_ERROR);
    assert.equal(byId.get(2)?.code, RPC_INVALID_REQUEST);
    assert.equal(byId.get(3)?.code, RPC_METHOD_NOT_FOUND);
    assert.equal(byId.get(4)?.code, RPC_DOMAIN_ERROR);
    assert.deepEqual(byId.get(4)?.data, { run_id: "x", code: "RUN_NOT_FOUND" });
    assert.equal(byId.get(5)?.code, RPC_INTERNAL_ERROR);
  });

  test("client: typed errors carry code and data", async () => {
    const pair = await startPair(echoHandlers);
    const c = await pair.client();
    assert.equal(await c.call("add", { a: 1, b: 1 }), 2);
    const err = await c.call("fail_domain").catch((e: unknown) => e);
    assert.ok(err instanceof RpcError);
    assert.equal(err.code, RPC_DOMAIN_ERROR);
    assert.equal(domainCode(err), "RUN_NOT_FOUND");
    const missing = await c.call("nope").catch((e: unknown) => e);
    assert.equal((missing as RpcError).code, RPC_METHOD_NOT_FOUND);
    assert.equal(domainCode(missing), undefined);
    c.close();
  });

  test("notifications: server to client and client to server", async () => {
    let seen: unknown;
    const pair = await startPair({
      ...echoHandlers,
      note: (params) => {
        seen = params;
        return null;
      },
    });
    const c = await pair.client();
    const got: { method: string; params: unknown }[] = [];
    const off = c.onNotification((n) => got.push(n));
    assert.equal(await c.call("tick"), "ticked");
    assert.deepEqual(got, [
      { method: "progress", params: { n: 1 } },
      { method: "progress", params: { n: 2 } },
    ]);
    off();
    c.notify("note", { hello: 1 });
    await pause(30);
    assert.deepEqual(seen, { hello: 1 });
    c.close();
  });

  test("client: per-request timeout", async () => {
    const pair = await startPair(echoHandlers);
    const c = await pair.client(60);
    const started = Date.now();
    const err = await c.call("never").catch((e: unknown) => e);
    assert.ok(err instanceof RpcError);
    assert.equal(err.code, RPC_CLIENT_TIMEOUT);
    assert.ok(Date.now() - started < 1000);
    // A per-call override beats the default.
    const quick = await c.call("never", undefined, { timeoutMs: 20 }).catch((e: unknown) => e);
    assert.equal((quick as RpcError).code, RPC_CLIENT_TIMEOUT);
    c.close();
  });

  test("disconnect: pending calls reject and the handler signal aborts", async () => {
    let signal: AbortSignal | undefined;
    const pair = await startPair({
      hang: (_p, ctx) => {
        signal = ctx.signal;
        return new Promise(() => {});
      },
    });
    const c = await pair.client(0);
    const pending = c.call("hang");
    await pause(30);
    assert.ok(signal && !signal.aborted);
    c.close();
    const err = await pending.catch((e: unknown) => e);
    assert.equal((err as RpcError).code, RPC_CLIENT_DISCONNECTED);
    await pause(30);
    assert.equal(signal?.aborted, true);
    const late = await c.call("echo").catch((e: unknown) => e);
    assert.equal((late as RpcError).code, RPC_CLIENT_DISCONNECTED);
  });

  test("guard: refuses a method before the handshake", async () => {
    let greeted = false;
    const pair = await startPair(
      {
        hello: () => {
          greeted = true;
          return "hi";
        },
        echo: (p) => p,
      },
      {
        guard: (method) => {
          if (!greeted && method !== "hello") {
            throw new RpcError(RPC_INVALID_REQUEST, "hello first");
          }
        },
      },
    );
    const c = await pair.client();
    const early = await c.call("echo", 1).catch((e: unknown) => e);
    assert.equal((early as RpcError).code, RPC_INVALID_REQUEST);
    assert.equal(await c.call("hello"), "hi");
    assert.equal(await c.call("echo", 1), 1);
    c.close();
  });
});

test("LineFramer reassembles a multi-byte UTF-8 character split across Buffer chunks", () => {
  const line = JSON.stringify({ t: "héllo — ✅ жизнь" }) + "\n";
  const raw = Buffer.from(line, "utf8");
  for (let cut = 1; cut < raw.length; cut++) {
    const f = new LineFramer();
    const got = [...f.push(raw.subarray(0, cut)), ...f.push(raw.subarray(cut))];
    assert.deepEqual(got, [line.trimEnd()], `split at byte ${cut}`);
  }
});
