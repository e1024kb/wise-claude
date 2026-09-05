// Client side of the daemon socket (P5): typed `call`, version handshake, detached auto-start.
// Used by the CLI (`wise-engine daemon ...`) and, later, by the MCP server (D13).

import { spawn } from "node:child_process";
import { closeSync, mkdirSync, openSync } from "node:fs";
import { createConnection } from "node:net";
import type { Socket } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { daemonPaths, readLock, rotateLog } from "./daemon.ts";
import type { DaemonPathOpts } from "./daemon.ts";
import type { HelloParams, HelloResult, MethodName, ParamsOf, ResultOf } from "./protocol.ts";
import { domainCode, RpcClient } from "./rpc.ts";
import type { CallOptions, NotificationListener } from "./rpc.ts";
import { pluginVersion } from "./version.ts";

// ---- options and errors --------------------------------------------------------------------

export type ClientOptions = DaemonPathOpts & {
  /** Version sent in `hello`; defaults to the plugin version. */
  version?: string;
  /** Client name sent in `hello`. */
  client?: string;
  /** Default per-call timeout in ms (`0` disables). Long `wait` calls pass their own. */
  timeoutMs?: number;
  connectTimeoutMs?: number;
  /** How long `ensureDaemon` polls for a freshly spawned socket. Default 5000. */
  startTimeoutMs?: number;
  /** How long `stopDaemon` waits for the socket to disappear. Default 5000. */
  stopTimeoutMs?: number;
  /** Idle timeout forwarded to a spawned daemon (ms). */
  idleMs?: number;
  /** Script + args run by `process.execPath` for the detached start. Default: `cli.ts daemon serve`. */
  entry?: string[];
};

export type ConnectErrorCode = "DAEMON_UNAVAILABLE" | "CONNECT_TIMEOUT" | "START_TIMEOUT";

/** The socket is dead, refused, or the daemon never came up. Not an RPC-level failure. */
export class ConnectError extends Error {
  readonly code: ConnectErrorCode;
  readonly cause_code: string | undefined;
  constructor(code: ConnectErrorCode, message: string, causeCode?: string) {
    super(message);
    this.name = "ConnectError";
    this.code = code;
    this.cause_code = causeCode;
  }
}

export type Client = {
  call: <M extends MethodName>(
    method: M,
    params: ParamsOf<M>,
    opts?: CallOptions,
  ) => Promise<ResultOf<M>>;
  close: () => void;
  notifications: { on: (listener: NotificationListener) => () => void };
  hello: HelloResult;
  socketPath: string;
};

// ---- raw socket --------------------------------------------------------------------------------

function rawConnect(socketPath: string, timeoutMs: number): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const socket = createConnection(socketPath);
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new ConnectError("CONNECT_TIMEOUT", `connect to ${socketPath} timed out`));
    }, timeoutMs);
    socket.once("connect", () => {
      clearTimeout(timer);
      resolve(socket);
    });
    socket.once("error", (err: NodeJS.ErrnoException) => {
      clearTimeout(timer);
      reject(new ConnectError("DAEMON_UNAVAILABLE", `${socketPath}: ${err.message}`, err.code));
    });
  });
}

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function socketAlive(socketPath: string, timeoutMs: number): Promise<boolean> {
  try {
    const s = await rawConnect(socketPath, timeoutMs);
    s.destroy();
    return true;
  } catch {
    return false;
  }
}

function wrap(rpc: RpcClient, hello: HelloResult, socketPath: string): Client {
  return {
    call: (method, params, opts) => rpc.call(method, params, opts) as Promise<never>,
    close: () => rpc.close(),
    notifications: { on: (l) => rpc.onNotification(l) },
    hello,
    socketPath,
  };
}

// ---- public API ------------------------------------------------------------------------------------

/**
 * Connect and shake hands. Throws `ConnectError` when the socket is dead and an `RpcError` with
 * `data.code === "DAEMON_VERSION_MISMATCH"` when the daemon runs another version.
 */
export async function connect(opts: ClientOptions = {}): Promise<Client> {
  const paths = daemonPaths(opts);
  const connectTimeoutMs = opts.connectTimeoutMs ?? 2000;
  const socket = await rawConnect(paths.socketPath, connectTimeoutMs);
  const rpc = new RpcClient(socket, { timeoutMs: opts.timeoutMs ?? 30_000 });
  const params: HelloParams = {
    version: opts.version ?? pluginVersion(),
    client: opts.client ?? "wise-engine",
  };
  try {
    const hello = (await rpc.call("hello", params, { timeoutMs: connectTimeoutMs })) as HelloResult;
    return wrap(rpc, hello, paths.socketPath);
  } catch (err) {
    rpc.close();
    throw err;
  }
}

/** Detached start: `process.execPath <entry> --data-root ... --socket ...`, stdio to the log. */
export function spawnDaemon(opts: ClientOptions = {}): { pid: number | undefined } {
  const paths = daemonPaths(opts);
  mkdirSync(paths.dataRoot, { recursive: true, mode: 0o700 });
  rotateLog(paths.logPath);
  const fd = openSync(paths.logPath, "a", 0o600);
  const entry = opts.entry ?? [
    join(dirname(fileURLToPath(import.meta.url)), "cli.ts"),
    "daemon",
    "serve",
  ];
  const args = [
    ...entry,
    "--data-root",
    paths.dataRoot,
    "--socket",
    paths.socketPath,
    "--lock",
    paths.lockPath,
    "--log",
    paths.logPath,
    ...(opts.idleMs !== undefined ? ["--idle-ms", String(opts.idleMs)] : []),
  ];
  try {
    const child = spawn(process.execPath, args, {
      detached: true,
      stdio: ["ignore", fd, fd],
      env: { ...(opts.env ?? process.env) } as NodeJS.ProcessEnv,
    });
    child.on("error", () => {
      // Spawn failure surfaces as START_TIMEOUT in ensureDaemon; nothing to do here.
    });
    child.unref();
    return { pid: child.pid };
  } finally {
    closeSync(fd);
  }
}

/**
 * Connect, spawning the daemon when the socket is dead. A version mismatch asks the old daemon to
 * exit when idle, waits for the socket to go away, then starts the current version.
 */
export async function ensureDaemon(opts: ClientOptions = {}): Promise<Client> {
  try {
    return await connect(opts);
  } catch (err) {
    if (domainCode(err) === "DAEMON_VERSION_MISMATCH") {
      const res = await stopDaemon(opts);
      if (!res.stopped) throw err;
    } else if (!(err instanceof ConnectError)) {
      throw err;
    }
  }
  const paths = daemonPaths(opts);
  spawnDaemon(opts);
  const startTimeoutMs = opts.startTimeoutMs ?? 5000;
  const deadline = Date.now() + startTimeoutMs;
  let lastError: unknown;
  while (Date.now() < deadline) {
    try {
      return await connect(opts);
    } catch (err) {
      if (!(err instanceof ConnectError)) throw err;
      lastError = err;
    }
    await pause(100);
  }
  throw new ConnectError(
    "START_TIMEOUT",
    `wise-engined did not answer on ${paths.socketPath} within ${startTimeoutMs} ms` +
      (lastError instanceof Error ? ` (${lastError.message}); see ${paths.logPath}` : ""),
  );
}

export type DaemonStatus = {
  alive: boolean;
  socketPath: string;
  /** Pid in the lock file, whether or not it answers. */
  lockPid: number | null;
  pid?: number;
  version?: string;
  started_at?: string;
  version_mismatch?: boolean;
};

/** Is a daemon answering on the socket, and which one. Never throws. */
export async function daemonStatus(opts: ClientOptions = {}): Promise<DaemonStatus> {
  const paths = daemonPaths(opts);
  const status: DaemonStatus = {
    alive: false,
    socketPath: paths.socketPath,
    lockPid: readLock(paths.lockPath),
  };
  try {
    const client = await connect(opts);
    client.close();
    status.alive = true;
    status.pid = client.hello.pid;
    status.version = client.hello.version;
    status.started_at = client.hello.started_at;
  } catch (err) {
    if (domainCode(err) === "DAEMON_VERSION_MISMATCH") {
      const data = (err as { data?: { daemon_version?: string; pid?: number } }).data ?? {};
      status.alive = true;
      status.version_mismatch = true;
      if (typeof data.daemon_version === "string") status.version = data.daemon_version;
      if (typeof data.pid === "number") status.pid = data.pid;
    }
  }
  return status;
}

export type StopResult = { stopped: boolean; was_running: boolean; active_runs: number };

/** Ask the daemon to exit (when idle by default, `now` to force) and wait for the socket to die. */
export async function stopDaemon(
  opts: ClientOptions & { now?: boolean } = {},
): Promise<StopResult> {
  const paths = daemonPaths(opts);
  const connectTimeoutMs = opts.connectTimeoutMs ?? 2000;
  let socket: Socket;
  try {
    socket = await rawConnect(paths.socketPath, connectTimeoutMs);
  } catch {
    return { stopped: true, was_running: false, active_runs: 0 };
  }
  const rpc = new RpcClient(socket, { timeoutMs: connectTimeoutMs });
  let activeRuns = 0;
  try {
    // `shutdown` is allowed before `hello`, so a version mismatch cannot block it.
    const res = (await rpc.call("shutdown", { when: opts.now ? "now" : "idle" })) as {
      active_runs?: number;
    };
    activeRuns = res.active_runs ?? 0;
  } finally {
    rpc.close();
  }
  const deadline = Date.now() + (opts.stopTimeoutMs ?? 5000);
  while (Date.now() < deadline) {
    if (!(await socketAlive(paths.socketPath, connectTimeoutMs))) {
      return { stopped: true, was_running: true, active_runs: activeRuns };
    }
    await pause(50);
  }
  return { stopped: false, was_running: true, active_runs: activeRuns };
}
