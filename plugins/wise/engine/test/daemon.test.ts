import { after, describe, test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { connect, ConnectError, daemonStatus, ensureDaemon, stopDaemon } from "../src/client.ts";
import type { Client } from "../src/client.ts";
import {
  childPath,
  clearChild,
  daemonCommand,
  daemonPaths,
  DaemonError,
  readChild,
  readLock,
  recordChild,
  startDaemon,
} from "../src/daemon.ts";
import type { Daemon, DaemonOptions } from "../src/daemon.ts";
import {
  appendEvent,
  initState,
  newUlid,
  readEvents,
  readState,
  startRun,
  startStep,
  updateRun,
  writeState,
} from "../src/ledger.ts";
import { RPC_INVALID_PARAMS, RPC_INVALID_REQUEST } from "../src/protocol.ts";
import { domainCode, RpcClient, RpcError } from "../src/rpc.ts";
import type { Gate } from "../src/types.ts";
import { createConnection } from "node:net";
import type { Socket } from "node:net";

const VERSION = "9.9.9-test";
const roots: string[] = [];
const daemons = new Set<Daemon>();
const clients = new Set<Client>();

type Root = { root: string; env: Record<string, string>; dataRoot: string };

/** Fresh data root; socket lands at `<tmp>/wd-XXXX/wise/engined.sock`, well under the 104-byte cap. */
function mkRoot(): Root {
  const root = mkdtempSync(join(tmpdir(), "wd-"));
  roots.push(root);
  const env = { XDG_DATA_HOME: root, HOME: root };
  const paths = daemonPaths({ env });
  assert.ok(paths.socketPath.length < 100, `socket path too long: ${paths.socketPath}`);
  return { root, env, dataRoot: paths.dataRoot };
}

async function start(r: Root, extra: Partial<DaemonOptions> = {}): Promise<Daemon> {
  const d = await startDaemon({ env: r.env, version: VERSION, idleMs: 60_000, ...extra });
  daemons.add(d);
  return d;
}

async function open(r: Root, version = VERSION): Promise<Client> {
  const c = await connect({ env: r.env, version, client: "test", timeoutMs: 5000 });
  clients.add(c);
  return c;
}

function rawSocket(path: string): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const s = createConnection(path);
    s.once("connect", () => resolve(s));
    s.once("error", reject);
  });
}

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** A run dir under `<dataRoot>/runs/<slug>/<ulid>` in `running` state with step `a` running. */
function makeRun(r: Root, slug = "ws"): { runDir: string; runId: string } {
  const runId = newUlid();
  const runDir = join(r.dataRoot, "runs", slug, runId);
  initState({
    runDir,
    runId,
    workflow: { name: "wf", version: 2, dir: r.root },
    stepIds: ["a", "b"],
    cwd: r.root,
  });
  startRun(runDir, {});
  startStep(runDir, "a");
  return { runDir, runId };
}

/** Pid of a process that has already exited. */
function deadPid(): number {
  const res = spawnSync(process.execPath, ["-e", "0"]);
  assert.ok(res.pid > 0);
  return res.pid;
}

describe("daemon", () => {
  after(async () => {
    for (const c of clients) c.close();
    for (const d of daemons) await d.close();
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  test("handshake: hello answers version, pid, started_at; socket is 0600; lock holds the pid", async () => {
    const r = mkRoot();
    const d = await start(r);
    const c = await open(r);
    assert.equal(c.hello.version, VERSION);
    assert.equal(c.hello.pid, process.pid);
    assert.match(c.hello.started_at, /^\d{4}-\d{2}-\d{2}T/);
    assert.deepEqual(await c.call("status", {}), []);
    assert.equal(statSync(d.runtime.paths.socketPath).mode & 0o777, 0o600);
    assert.equal(readLock(d.runtime.paths.lockPath), process.pid);
    assert.equal(d.connections(), 1);
    c.close();
    await pause(30);
    assert.equal(d.connections(), 0);
  });

  test("handshake: version mismatch is a domain error and status reports it", async () => {
    const r = mkRoot();
    await start(r);
    const err = await connect({ env: r.env, version: "0.0.1", client: "old" }).catch(
      (e: unknown) => e,
    );
    assert.ok(err instanceof RpcError);
    assert.equal(domainCode(err), "DAEMON_VERSION_MISMATCH");
    assert.equal((err.data as { daemon_version: string }).daemon_version, VERSION);
    const st = await daemonStatus({ env: r.env, version: "0.0.1" });
    assert.equal(st.alive, true);
    assert.equal(st.version_mismatch, true);
    assert.equal(st.version, VERSION);
    assert.equal(st.pid, process.pid);
  });

  test("handshake: methods before hello are refused, shutdown is allowed", async () => {
    const r = mkRoot();
    const d = await start(r);
    const rpc = new RpcClient(await rawSocket(d.runtime.paths.socketPath), { timeoutMs: 2000 });
    const early = await rpc.call("status", {}).catch((e: unknown) => e);
    assert.equal((early as RpcError).code, RPC_INVALID_REQUEST);
    const res = (await rpc.call("shutdown", { when: "now" })) as { accepted: boolean };
    assert.equal(res.accepted, true);
    rpc.close();
    assert.equal(await d.closed, "shutdown");
    assert.equal(existsSync(d.runtime.paths.socketPath), false);
    assert.equal(existsSync(d.runtime.paths.lockPath), false);
  });

  test("single instance: two concurrent starts yield one daemon", async () => {
    const r = mkRoot();
    const results = await Promise.allSettled([start(r), start(r)]);
    const ok = results.filter((x) => x.status === "fulfilled");
    const failed = results.filter((x) => x.status === "rejected");
    assert.equal(ok.length, 1);
    assert.equal(failed.length, 1);
    const err = (failed[0] as PromiseRejectedResult).reason as unknown;
    assert.ok(err instanceof DaemonError);
    assert.equal(err.code, "ALREADY_RUNNING");
    assert.equal(err.pid, process.pid);
    // The loser must not have removed the winner's lock.
    assert.equal(readLock(daemonPaths({ env: r.env }).lockPath), process.pid);
  });

  test("single instance: a stale lock with a dead pid is taken over", async () => {
    const r = mkRoot();
    const paths = daemonPaths({ env: r.env });
    const stale = deadPid();
    mkdirSync(paths.dataRoot, { recursive: true });
    writeFileSync(paths.lockPath, `${stale}\n`);
    writeFileSync(paths.socketPath, "not a socket");
    const d = await start(r);
    assert.equal(readLock(paths.lockPath), process.pid);
    const c = await open(r);
    assert.equal(c.hello.pid, process.pid);
    c.close();
    await d.close();
    assert.equal(existsSync(paths.lockPath), false);
  });

  test("idle exit removes socket and lock", async () => {
    const r = mkRoot();
    const d = await start(r, { idleMs: 300 });
    const c = await open(r);
    await pause(400);
    // A connected client keeps it alive past idleMs.
    assert.equal(existsSync(d.runtime.paths.socketPath), true);
    c.close();
    assert.equal(await d.closed, "idle");
    assert.equal(existsSync(d.runtime.paths.socketPath), false);
    assert.equal(existsSync(d.runtime.paths.lockPath), false);
    const err = await connect({ env: r.env, version: VERSION }).catch((e: unknown) => e);
    assert.ok(err instanceof ConnectError);
    assert.equal(err.code, "DAEMON_UNAVAILABLE");
  });

  test("idle exit waits while a run is active", async () => {
    const r = mkRoot();
    const { runDir, runId } = makeRun(r);
    const d = await start(r, { idleMs: 200 });
    // Recovery paused it; flip it back to running through the handler.
    const c = await open(r);
    await c.call("resume", { run_id: runId });
    c.close();
    await pause(450);
    assert.equal(existsSync(d.runtime.paths.socketPath), true, "active run keeps the daemon up");
    assert.equal(d.activeRuns(), 1);
    updateRun(runDir, { status: "completed" });
    assert.equal(await d.closed, "idle");
  });

  test("stubs answer NOT_IMPLEMENTED and injected handlers replace them", async () => {
    const r = mkRoot();
    const d = await start(r);
    const c = await open(r);
    for (const method of ["preflight", "run", "answer", "report"] as const) {
      const err = await c.call(method, { run_id: "x" } as never).catch((e: unknown) => e);
      assert.equal(domainCode(err), "NOT_IMPLEMENTED", method);
    }
    c.close();
    await d.close();

    const r2 = mkRoot();
    let seenRuntimeVersion = "";
    await start(r2, {
      handlers: (rt) => {
        seenRuntimeVersion = rt.version;
        return {
          preflight: (p) => ({
            workflow: p.workflow,
            version: 2,
            questions: [],
            defaults: {},
            requires_missing: [],
          }),
        };
      },
    });
    const c2 = await open(r2);
    const res = await c2.call("preflight", { workflow: "wf", cwd: r2.root });
    assert.equal(res.workflow, "wf");
    assert.equal(seenRuntimeVersion, VERSION);
    assert.equal(
      domainCode(await c2.call("report", { run_id: "x" }).catch((e: unknown) => e)),
      "NOT_IMPLEMENTED",
    );
    c2.close();
  });

  test("status: one run, all runs across workspaces, unknown run", async () => {
    const r = mkRoot();
    const a = makeRun(r, "ws-a");
    const b = makeRun(r, "ws-b");
    await start(r);
    const c = await open(r);
    const one = await c.call("status", { run_id: a.runId });
    assert.ok(!Array.isArray(one));
    assert.equal(one.run_id, a.runId);
    assert.equal(one.status, "paused");
    assert.equal(one.workflow, "wf");
    const all = await c.call("status", {});
    assert.ok(Array.isArray(all));
    assert.deepEqual(all.map((s) => s.run_id).toSorted(), [a.runId, b.runId].toSorted());
    const err = await c.call("status", { run_id: "01NOPE" }).catch((e: unknown) => e);
    assert.equal(domainCode(err), "RUN_NOT_FOUND");
    const bad = await c.call("status", { run_id: "../x" }).catch((e: unknown) => e);
    assert.equal(domainCode(bad), "RUN_NOT_FOUND");
    c.close();
  });

  test("wait: times out with no events, returns early on a new event", async () => {
    const r = mkRoot();
    const { runDir, runId } = makeRun(r);
    await start(r, { wait: { pollMs: 20 } });
    const c = await open(r);
    await c.call("resume", { run_id: runId });
    const resumed = readEvents(runDir);
    const since = resumed.at(-1)?.seq ?? 0;

    const t0 = Date.now();
    const empty = await c.call("wait", { run_id: runId, after: since, timeout_ms: 150 });
    assert.ok(Date.now() - t0 >= 140, "waited for the timeout");
    assert.deepEqual(empty, { events: [], status: "running", done: false });

    const t1 = Date.now();
    const pending = c.call("wait", { run_id: runId, after: since, timeout_ms: 5000 });
    setTimeout(() => appendEvent(runDir, { run_id: runId, type: "step.progress", step: "a" }), 60);
    const early = await pending;
    assert.ok(Date.now() - t1 < 2000, "returned before the timeout");
    assert.equal(early.events.length, 1);
    assert.equal(early.events[0]?.type, "step.progress");
    assert.equal(early.events[0]?.seq, since + 1);
    assert.equal(early.done, false);

    // Terminal status returns at once with done: true.
    updateRun(runDir, { status: "completed" });
    const done = await c.call("wait", { run_id: runId, after: since + 1, timeout_ms: 5000 });
    assert.equal(done.done, true);
    assert.equal(done.status, "completed");
    c.close();
  });

  test("wait: returns the gate when the run is gated", async () => {
    const r = mkRoot();
    const { runDir, runId } = makeRun(r);
    await start(r);
    const gate: Gate = {
      gate_id: "g1",
      step: "a",
      kind: "approval",
      message: "ship it?",
      options: [
        { value: "approve", label: "Approve" },
        { value: "reject", label: "Reject" },
      ],
    };
    const state = readState(runDir);
    state.status = "gated";
    state.gate = gate;
    writeState(runDir, state);
    const c = await open(r);
    const since = readEvents(runDir).at(-1)?.seq ?? 0;
    const t0 = Date.now();
    const res = await c.call("wait", { run_id: runId, after: since, timeout_ms: 5000 });
    assert.ok(Date.now() - t0 < 1000);
    assert.deepEqual(res, { events: [], status: "gated", gate, done: false });
    c.close();
  });

  test("wait: sends progress notifications while blocked", async () => {
    const r = mkRoot();
    const { runId } = makeRun(r);
    await start(r, { wait: { pollMs: 10, progressMs: 40 } });
    const c = await open(r);
    await c.call("resume", { run_id: runId });
    const seen: { method: string; params: unknown }[] = [];
    c.notifications.on((n) => seen.push(n));
    const since = 99; // past every existing seq
    await c.call("wait", { run_id: runId, after: since, timeout_ms: 200 });
    assert.ok(seen.length >= 2, `expected progress notifications, got ${seen.length}`);
    assert.equal(seen[0]?.method, "progress");
    const p = seen[0]?.params as { run_id: string; waiting_ms: number };
    assert.equal(p.run_id, runId);
    assert.ok(p.waiting_ms >= 30);
    c.close();
  });

  test("wait: bad params", async () => {
    const r = mkRoot();
    await start(r);
    const c = await open(r);
    const err = await c.call("wait", { run_id: "" }).catch((e: unknown) => e);
    assert.equal((err as RpcError).code, RPC_INVALID_PARAMS);
    c.close();
  });

  test("cancel flips status, cancels running steps, appends run.done, clears the child record", async () => {
    const r = mkRoot();
    const { runDir, runId } = makeRun(r);
    await start(r);
    const c = await open(r);
    await c.call("resume", { run_id: runId });
    startStep(runDir, "b");
    recordChild(runDir, { pgid: deadPid(), pid: 1 });
    const res = await c.call("cancel", { run_id: runId, reason: "operator" });
    assert.deepEqual(res, { status: "cancelled" });
    const s = readState(runDir);
    assert.equal(s.status, "cancelled");
    assert.equal(s.steps.b?.status, "cancelled");
    assert.equal(s.steps.a?.status, "pending");
    assert.ok(s.completed_at);
    assert.equal(existsSync(childPath(runDir)), false);
    const last = readEvents(runDir).at(-1);
    assert.equal(last?.type, "run.done");
    assert.equal(last?.verdict, "cancelled");
    assert.equal(last?.message, "operator");
    const again = await c.call("cancel", { run_id: runId });
    assert.deepEqual(again, { status: "cancelled" });
    const summary = await c.call("status", { run_id: runId });
    assert.equal((summary as { status: string }).status, "cancelled");
    // resume of a cancelled run is refused
    const err = await c.call("resume", { run_id: runId }).catch((e: unknown) => e);
    assert.equal((err as RpcError).code, RPC_INVALID_PARAMS);
    c.close();
  });

  test("resume: paused run goes back to running; gated and completed are refused", async () => {
    const r = mkRoot();
    const { runDir, runId } = makeRun(r);
    await start(r);
    const c = await open(r);
    assert.equal(readState(runDir).status, "paused");
    const res = await c.call("resume", { run_id: runId });
    assert.deepEqual(res, { run_id: runId, status: "running" });
    const s = readState(runDir);
    assert.equal(s.status, "running");
    assert.equal(s.steps.a?.status, "pending");
    assert.equal(readEvents(runDir).at(-1)?.message, "run resumed");
    // running again is a no-op
    assert.deepEqual(await c.call("resume", { run_id: runId }), {
      run_id: runId,
      status: "running",
    });

    updateRun(runDir, { status: "gated" });
    const gated = await c.call("resume", { run_id: runId }).catch((e: unknown) => e);
    assert.equal((gated as RpcError).code, RPC_INVALID_PARAMS);
    updateRun(runDir, { status: "completed" });
    const done = await c.call("resume", { run_id: runId }).catch((e: unknown) => e);
    assert.equal((done as RpcError).code, RPC_INVALID_PARAMS);
    c.close();
  });

  test("crash recovery: running runs are paused, running steps reset, child record cleared", async () => {
    const r = mkRoot();
    const { runDir, runId } = makeRun(r);
    recordChild(runDir, { pgid: deadPid(), pid: 42 });
    const done = makeRun(r);
    updateRun(done.runDir, { status: "completed" });
    const before = readEvents(runDir).length;
    await start(r);
    const s = readState(runDir);
    assert.equal(s.status, "paused");
    assert.equal(s.steps.a?.status, "pending");
    assert.equal(s.steps.a?.step_run_id, undefined);
    assert.equal(s.steps.b?.status, "pending");
    const events = readEvents(runDir);
    assert.equal(events.length, before + 1);
    assert.equal(events.at(-1)?.type, "warn");
    assert.equal(events.at(-1)?.message, "daemon restarted, run paused");
    assert.equal(events.at(-1)?.run_id, runId);
    assert.equal(existsSync(childPath(runDir)), false);
    assert.equal(readState(done.runDir).status, "completed", "terminal runs untouched");
  });

  test("child sidecar: recordChild / readChild / clearChild", () => {
    const r = mkRoot();
    const runDir = join(r.dataRoot, "runs", "ws", "01RUN");
    const rec = recordChild(runDir, { pgid: 1234, pid: 1235 });
    assert.equal(rec.pgid, 1234);
    assert.match(rec.started_at, /Z$/);
    assert.deepEqual(readChild(runDir), rec);
    assert.deepEqual(JSON.parse(readFileSync(childPath(runDir), "utf8")), rec);
    clearChild(runDir);
    assert.equal(readChild(runDir), null);
    clearChild(runDir);
  });

  test("stopDaemon: idle shutdown removes the socket; a dead socket reports not running", async () => {
    const r = mkRoot();
    const d = await start(r);
    const res = await stopDaemon({ env: r.env, version: VERSION });
    assert.deepEqual(res, { stopped: true, was_running: true, active_runs: 0 });
    assert.equal(await d.closed, "shutdown");
    assert.equal(existsSync(d.runtime.paths.socketPath), false);
    const again = await stopDaemon({ env: r.env, version: VERSION });
    assert.equal(again.was_running, false);
    const st = await daemonStatus({ env: r.env, version: VERSION });
    assert.equal(st.alive, false);
    assert.equal(st.lockPid, null);
  });

  test("daemonCommand: status, help, unknown", async () => {
    const r = mkRoot();
    const out: string[] = [];
    const err: string[] = [];
    const io = { out: (s: string) => out.push(s), err: (s: string) => err.push(s), env: r.env };
    assert.equal(await daemonCommand(["status", "--json"], io), 1);
    const st = JSON.parse(out[0] ?? "{}") as { alive: boolean; socketPath: string };
    assert.equal(st.alive, false);
    assert.equal(st.socketPath, daemonPaths({ env: r.env }).socketPath);
    assert.equal(await daemonCommand(["help"], io), 0);
    assert.match(out[1] ?? "", /serve/);
    assert.equal(await daemonCommand(["bogus"], io), 64);
    assert.match(err[0] ?? "", /unknown subcommand/);
    // Stopping a daemon that is not running is idempotent.
    assert.equal(await daemonCommand(["stop"], io), 0);
    assert.match(out.at(-1) ?? "", /not running/);
  });

  test(
    "ensureDaemon: detached spawn with process.execPath, then stop",
    { timeout: 30_000 },
    async () => {
      const r = mkRoot();
      const entry = [
        join(dirname(fileURLToPath(import.meta.url)), "..", "src", "daemon.ts"),
        "serve",
      ];
      const opts = { env: r.env, entry, idleMs: 3000, startTimeoutMs: 15_000, client: "test" };
      const c = await ensureDaemon(opts);
      try {
        assert.notEqual(c.hello.pid, process.pid);
        assert.deepEqual(await c.call("status", {}), []);
        const paths = daemonPaths({ env: r.env });
        assert.equal(readLock(paths.lockPath), c.hello.pid);
        assert.equal(statSync(paths.socketPath).mode & 0o777, 0o600);
        // A second ensureDaemon reuses the running one.
        const c2 = await ensureDaemon(opts);
        assert.equal(c2.hello.pid, c.hello.pid);
        c2.close();
        assert.ok(existsSync(paths.logPath));
      } finally {
        c.close();
      }
      const res = await stopDaemon({ ...opts, stopTimeoutMs: 10_000 });
      assert.equal(res.stopped, true);
      assert.equal(res.was_running, true);
      const paths = daemonPaths({ env: r.env });
      // The detached process removes its lock on the way out.
      for (let i = 0; i < 100 && existsSync(paths.lockPath); i++) await pause(50);
      assert.equal(existsSync(paths.lockPath), false);
      assert.match(readFileSync(paths.logPath, "utf8"), /engined: listening/);
    },
  );
});
