// wise-engined: the detached daemon (docs/wise/research-ts-engine.md P5, D13, D17).
// Listens on a Unix socket, speaks the P1 JSON-RPC vocabulary (protocol.ts, rpc.ts), owns the
// single-instance lock, crash recovery and idle exit. Run execution (M2.3) plugs in through
// `DaemonHandlers`; the ledger-backed methods (`status`, `wait`, `cancel`, `resume`) live here.

import {
  chmodSync,
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
  writeSync,
} from "node:fs";
import { createServer } from "node:net";
import type { Server, Socket } from "node:net";
import { dirname, join } from "node:path";
import {
  appendEvent,
  readEvents,
  readState,
  resetRunning,
  statePath,
  utcNow,
  writeState,
} from "./ledger.ts";
import { wiseDataRoot } from "./paths.ts";
import type { Env } from "./paths.ts";
import {
  RPC_INVALID_PARAMS,
  RPC_INVALID_REQUEST,
  WAIT_DEFAULT_MS,
  WAIT_MAX_MS,
  WAIT_POLL_MS,
  WAIT_PROGRESS_MS,
} from "./protocol.ts";
import type {
  HelloParams,
  HelloResult,
  MethodName,
  ParamsOf,
  ProgressParams,
  ResultOf,
  ShutdownParams,
  ShutdownResult,
  WaitResult,
} from "./protocol.ts";
import { domainError, RpcError, serveConnection } from "./rpc.ts";
import type { CallContext, RpcHandlerMap } from "./rpc.ts";
import type { Executor } from "./executor.ts";
import type { RunStatus, RunSummary, State } from "./types.ts";
import { buildId } from "./version.ts";

// ---- constants ---------------------------------------------------------------------------

export const IDLE_MS_DEFAULT = 30 * 60 * 1000;
export const LOG_ROTATE_BYTES = 10 * 1024 * 1024;
const RUN_ID_RE = /^[A-Za-z0-9_-]+$/;
/** Run statuses that keep the daemon alive. `gated` and `paused` are parked (P5). */
const ACTIVE_RUN: ReadonlySet<RunStatus> = new Set(["initializing", "running"]);
const DONE_RUN: ReadonlySet<RunStatus> = new Set(["completed", "failed", "cancelled"]);

// ---- paths ---------------------------------------------------------------------------------

export type DaemonPathOpts = {
  env?: Env;
  home?: string;
  dataRoot?: string;
  socketPath?: string;
  lockPath?: string;
  logPath?: string;
};

export type DaemonPaths = {
  dataRoot: string;
  runsRoot: string;
  socketPath: string;
  lockPath: string;
  logPath: string;
};

/** `$XDG_RUNTIME_DIR/wise/engined.sock` else `<dataRoot>/engined.sock`; lock and log under dataRoot. */
export function daemonPaths(opts: DaemonPathOpts = {}): DaemonPaths {
  const env = opts.env ?? process.env;
  const rootOpts = opts.home === undefined ? { env } : { env, home: opts.home };
  const dataRoot = opts.dataRoot ?? wiseDataRoot(rootOpts);
  const runtimeDir = env.XDG_RUNTIME_DIR;
  const socketPath =
    opts.socketPath ??
    (runtimeDir ? join(runtimeDir, "wise", "engined.sock") : join(dataRoot, "engined.sock"));
  return {
    dataRoot,
    runsRoot: join(dataRoot, "runs"),
    socketPath,
    lockPath: opts.lockPath ?? join(dataRoot, "engined.lock"),
    logPath: opts.logPath ?? join(dataRoot, "engined.log"),
  };
}

/** Rename `engined.log` to `engined.log.1` once it passes 10 MB. */
export function rotateLog(logPath: string, limit: number = LOG_ROTATE_BYTES): boolean {
  try {
    if (statSync(logPath).size <= limit) return false;
    renameSync(logPath, logPath + ".1");
    return true;
  } catch {
    return false;
  }
}

// ---- process helpers -------------------------------------------------------------------------

export function pidAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === "EPERM";
  }
}

function groupAlive(pgid: number): boolean {
  if (!Number.isInteger(pgid) || pgid <= 0) return false;
  try {
    process.kill(-pgid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === "EPERM";
  }
}

/** Signal a whole process group; `false` when it was already gone. */
export function killGroup(pgid: number, signal: NodeJS.Signals = "SIGTERM"): boolean {
  if (!Number.isInteger(pgid) || pgid <= 0) return false;
  try {
    process.kill(-pgid, signal);
    return true;
  } catch {
    return false;
  }
}

// ---- lock file ---------------------------------------------------------------------------------

export type DaemonErrorCode = "ALREADY_RUNNING" | "LOCK_FAILED";

export class DaemonError extends Error {
  readonly code: DaemonErrorCode;
  readonly pid: number | undefined;
  constructor(code: DaemonErrorCode, message: string, pid?: number) {
    super(message);
    this.name = "DaemonError";
    this.code = code;
    this.pid = pid;
  }
}

/** Pid recorded in the lock file, or `null` when absent or unreadable. */
export function readLock(lockPath: string): number | null {
  try {
    const pid = Number.parseInt(readFileSync(lockPath, "utf8").trim(), 10);
    return Number.isInteger(pid) && pid > 0 ? pid : null;
  } catch {
    return null;
  }
}

/**
 * `O_EXCL` create with our pid. An existing lock whose pid is alive means another daemon owns the
 * data root; a dead pid is stale and taken over. Node has no flock, this is the accepted substitute.
 */
function acquireLock(lockPath: string): void {
  mkdirSync(dirname(lockPath), { recursive: true, mode: 0o700 });
  for (let attempt = 0; attempt < 3; attempt++) {
    let fd: number;
    try {
      fd = openSync(lockPath, "wx", 0o600);
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "EEXIST") {
        throw new DaemonError("LOCK_FAILED", `cannot create ${lockPath}: ${String(err)}`);
      }
      const holder = readLock(lockPath);
      if (holder !== null && pidAlive(holder)) {
        throw new DaemonError(
          "ALREADY_RUNNING",
          `wise-engined already running (pid ${holder}, lock ${lockPath})`,
          holder,
        );
      }
      // Stale (dead pid or garbage): remove and retry the exclusive create.
      rmSync(lockPath, { force: true });
      continue;
    }
    try {
      writeSync(fd, `${process.pid}\n`);
    } finally {
      closeSync(fd);
    }
    return;
  }
  throw new DaemonError("LOCK_FAILED", `could not take ${lockPath} after 3 attempts`);
}

/** Remove the lock only while it still carries our pid. */
function releaseLock(lockPath: string): void {
  if (readLock(lockPath) === process.pid) rmSync(lockPath, { force: true });
}

// ---- child sidecar <runDir>/daemon.json ----------------------------------------------------------------

export type ChildRecord = { pgid: number; pid: number; started_at: string };

export function childPath(runDir: string): string {
  return join(runDir, "daemon.json");
}

function normalizeRecord(value: unknown): ChildRecord | null {
  if (typeof value !== "object" || value === null) return null;
  const parsed = value as Partial<ChildRecord>;
  if (typeof parsed.pgid !== "number" || typeof parsed.pid !== "number") return null;
  return {
    pgid: parsed.pgid,
    pid: parsed.pid,
    started_at: typeof parsed.started_at === "string" ? parsed.started_at : "",
  };
}

function writeChildren(runDir: string, children: ChildRecord[]): void {
  mkdirSync(runDir, { recursive: true });
  writeFileSync(childPath(runDir), JSON.stringify({ children }) + "\n", "utf8");
}

/**
 * Every live child process group recorded for the run. A run has many concurrent children
 * (units × phases, plus parallel agent steps), so the sidecar holds a list; the single-record
 * shape older builds wrote still reads back as a one-element list.
 */
export function readChildren(runDir: string): ChildRecord[] {
  try {
    const parsed = JSON.parse(readFileSync(childPath(runDir), "utf8")) as unknown;
    const list = (parsed as { children?: unknown }).children;
    if (Array.isArray(list)) {
      return list.map(normalizeRecord).filter((r): r is ChildRecord => r !== null);
    }
    const one = normalizeRecord(parsed);
    return one ? [one] : [];
  } catch {
    return [];
  }
}

/** Record the process group of a live child so cancel and restart can kill it. */
export function recordChild(
  runDir: string,
  child: { pgid: number; pid: number; started_at?: string },
): ChildRecord {
  const rec: ChildRecord = {
    pgid: child.pgid,
    pid: child.pid,
    started_at: child.started_at ?? utcNow(),
  };
  writeChildren(runDir, [...readChildren(runDir).filter((r) => r.pid !== rec.pid), rec]);
  return rec;
}

/** The most recently recorded live child, or `null`. */
export function readChild(runDir: string): ChildRecord | null {
  return readChildren(runDir).at(-1) ?? null;
}

/** Drop one child by pid, or every record when no pid is given. */
export function clearChild(runDir: string, pid?: number): void {
  if (pid === undefined) {
    rmSync(childPath(runDir), { force: true });
    return;
  }
  const left = readChildren(runDir).filter((r) => r.pid !== pid);
  if (left.length === 0) rmSync(childPath(runDir), { force: true });
  else writeChildren(runDir, left);
}

// ---- run lookup --------------------------------------------------------------------------------------

/** Every `<runsRoot>/<cwd-slug>/<run-id>` holding a state.json, across all workspaces. */
export function listRunDirs(runsRoot: string): string[] {
  const out: string[] = [];
  let slugs: string[];
  try {
    slugs = readdirSync(runsRoot);
  } catch {
    return out;
  }
  for (const slug of slugs.toSorted()) {
    const slugDir = join(runsRoot, slug);
    let ids: string[];
    try {
      ids = readdirSync(slugDir);
    } catch {
      continue;
    }
    for (const id of ids.toSorted()) {
      const dir = join(slugDir, id);
      if (existsSync(statePath(dir))) out.push(dir);
    }
  }
  return out;
}

export function findRunDir(runsRoot: string, runId: string): string | null {
  if (!RUN_ID_RE.test(runId)) return null;
  let slugs: string[];
  try {
    slugs = readdirSync(runsRoot);
  } catch {
    return null;
  }
  for (const slug of slugs) {
    const dir = join(runsRoot, slug, runId);
    if (existsSync(statePath(dir))) return dir;
  }
  return null;
}

export function summarize(state: State): RunSummary {
  const summary: RunSummary = {
    run_id: state.run_id,
    workflow: state.workflow?.name ?? "",
    status: state.status,
    started_at: state.started_at,
    last_activity_at: state.last_activity_at,
    cwd: state.cwd,
  };
  if (state.completed_at !== undefined) summary.completed_at = state.completed_at;
  if (state.gate) summary.gate = state.gate;
  return summary;
}

function readStateSafe(dir: string): State | null {
  try {
    return readState(dir);
  } catch {
    return null;
  }
}

// ---- crash recovery ---------------------------------------------------------------------------------------

export type RecoveryReport = { paused: string[]; killed: number[] };

/**
 * Ledger is truth: every `running` run gets its running steps reset to `pending`, is marked
 * `paused` with a `warn` event, and any recorded child process group still alive is killed.
 */
export function recoverRuns(
  runsRoot: string,
  log: (line: string) => void = () => {},
): RecoveryReport {
  const report: RecoveryReport = { paused: [], killed: [] };
  for (const dir of listRunDirs(runsRoot)) {
    for (const child of readChildren(dir)) {
      if (groupAlive(child.pgid) && killGroup(child.pgid)) report.killed.push(child.pgid);
    }
    clearChild(dir);
    const state = readStateSafe(dir);
    if (!state || state.status !== "running") continue;
    const reset = resetRunning(dir);
    reset.status = "paused";
    writeState(dir, reset);
    appendEvent(dir, {
      run_id: reset.run_id,
      type: "warn",
      message: "daemon restarted, run paused",
    });
    report.paused.push(reset.run_id);
    log(`recovery: run ${reset.run_id} paused`);
  }
  return report;
}

// ---- handlers ---------------------------------------------------------------------------------------------

export type Handler<M extends MethodName> = (
  params: ParamsOf<M>,
  ctx: CallContext,
) => Promise<ResultOf<M>> | ResultOf<M>;

/** The P1 method table the executor (M2.3) fills in; the daemon ships ledger-backed defaults. */
export type DaemonHandlers = {
  preflight: Handler<"preflight">;
  run: Handler<"run">;
  wait: Handler<"wait">;
  answer: Handler<"answer">;
  status: Handler<"status">;
  cancel: Handler<"cancel">;
  resume: Handler<"resume">;
  report: Handler<"report">;
  nudge: Handler<"nudge">;
  child_report: Handler<"child_report">;
  child_ask: Handler<"child_ask">;
  child_context: Handler<"child_context">;
  child_checkpoint: Handler<"child_checkpoint">;
};

/** What a handler factory and the executor get from the daemon. */
export type DaemonRuntime = {
  paths: DaemonPaths;
  version: string;
  pid: number;
  started_at: string;
  log: (line: string) => void;
  listRunDirs: () => string[];
  findRunDir: (runId: string) => string | null;
  /** Throws `RUN_NOT_FOUND`. */
  requireRunDir: (runId: string) => string;
};

export type HandlerSource =
  | Partial<DaemonHandlers>
  | ((runtime: DaemonRuntime) => Partial<DaemonHandlers>);

export type WaitTuning = { pollMs?: number; progressMs?: number };

function asRecord(params: unknown, method: string): Record<string, unknown> {
  if (typeof params !== "object" || params === null || Array.isArray(params)) {
    throw new RpcError(RPC_INVALID_PARAMS, `${method}: params must be an object`);
  }
  return params as Record<string, unknown>;
}

function requireString(rec: Record<string, unknown>, key: string, method: string): string {
  const v = rec[key];
  if (typeof v !== "string" || v === "") {
    throw new RpcError(RPC_INVALID_PARAMS, `${method}: ${key} must be a non-empty string`);
  }
  return v;
}

function optionalNumber(
  rec: Record<string, unknown>,
  key: string,
  method: string,
): number | undefined {
  const v = rec[key];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "number" || !Number.isFinite(v)) {
    throw new RpcError(RPC_INVALID_PARAMS, `${method}: ${key} must be a number`);
  }
  return v;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted || ms <= 0) {
      resolve();
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function waitSnapshot(runDir: string, after: number): WaitResult {
  const state = readState(runDir);
  const result: WaitResult = {
    events: readEvents(runDir, after),
    status: state.status,
    done: DONE_RUN.has(state.status),
  };
  if (state.status === "gated" && state.gate) result.gate = state.gate;
  return result;
}

function notImplemented<M extends MethodName>(method: M): Handler<M> {
  return () => {
    throw domainError("NOT_IMPLEMENTED", `${method}: not implemented in this daemon build`, {
      method,
    });
  };
}

/** Ledger-backed handlers; `preflight`, `run`, `answer`, `report` are stubs until M2.3. */
export function ledgerHandlers(rt: DaemonRuntime, tuning: WaitTuning = {}): DaemonHandlers {
  const pollMs = tuning.pollMs ?? WAIT_POLL_MS;
  const progressMs = tuning.progressMs ?? WAIT_PROGRESS_MS;

  const status: Handler<"status"> = (params) => {
    const rec = params === undefined ? {} : asRecord(params, "status");
    if (rec.run_id !== undefined) {
      const runId = requireString(rec, "run_id", "status");
      return summarize(readState(rt.requireRunDir(runId)));
    }
    const all: RunSummary[] = [];
    for (const dir of rt.listRunDirs()) {
      const s = readStateSafe(dir);
      if (s) all.push(summarize(s));
    }
    all.sort((a, b) =>
      a.last_activity_at < b.last_activity_at
        ? 1
        : a.last_activity_at > b.last_activity_at
          ? -1
          : 0,
    );
    return all;
  };

  const wait: Handler<"wait"> = async (params, ctx) => {
    const rec = asRecord(params, "wait");
    const runId = requireString(rec, "run_id", "wait");
    const after = optionalNumber(rec, "after", "wait") ?? 0;
    const requested = optionalNumber(rec, "timeout_ms", "wait") ?? WAIT_DEFAULT_MS;
    const timeoutMs = Math.min(Math.max(0, requested), WAIT_MAX_MS);
    const runDir = rt.requireRunDir(runId);
    const start = Date.now();
    let lastProgress = start;
    for (;;) {
      const snap = waitSnapshot(runDir, after);
      // Parked runs (`gated`, `paused`) return at once: nothing moves until a client acts.
      if (
        snap.events.length > 0 ||
        snap.done ||
        snap.status === "gated" ||
        snap.status === "paused"
      ) {
        return snap;
      }
      const elapsed = Date.now() - start;
      if (elapsed >= timeoutMs || ctx.signal.aborted) return snap;
      if (Date.now() - lastProgress >= progressMs) {
        const progress: ProgressParams = { run_id: runId, waiting_ms: elapsed };
        ctx.notify("progress", progress);
        lastProgress = Date.now();
      }
      await sleep(Math.min(pollMs, timeoutMs - elapsed), ctx.signal);
    }
  };

  const cancel: Handler<"cancel"> = (params) => {
    const rec = asRecord(params, "cancel");
    const runId = requireString(rec, "run_id", "cancel");
    const reason = typeof rec.reason === "string" ? rec.reason : undefined;
    const runDir = rt.requireRunDir(runId);
    const state = readState(runDir);
    if (state.status === "cancelled") return { status: "cancelled" };
    if (state.status === "completed") {
      throw new RpcError(RPC_INVALID_PARAMS, `cancel: run ${runId} is already completed`);
    }
    for (const child of readChildren(runDir)) killGroup(child.pgid);
    clearChild(runDir);
    const now = utcNow();
    for (const step of Object.values(state.steps)) {
      if (step.status === "running") {
        step.status = "cancelled";
        step.completed_at = now;
      }
    }
    state.status = "cancelled";
    state.completed_at = now;
    state.last_activity_at = now;
    delete state.gate;
    writeState(runDir, state);
    appendEvent(runDir, {
      run_id: state.run_id,
      type: "run.done",
      verdict: "cancelled",
      ...(reason ? { message: reason } : {}),
    });
    rt.log(`cancel: run ${runId}${reason ? ` (${reason})` : ""}`);
    return { status: "cancelled" };
  };

  const resume: Handler<"resume"> = (params) => {
    const rec = asRecord(params, "resume");
    const runId = requireString(rec, "run_id", "resume");
    const runDir = rt.requireRunDir(runId);
    const state = readState(runDir);
    if (DONE_RUN.has(state.status) && state.status !== "failed") {
      throw new RpcError(RPC_INVALID_PARAMS, `resume: run ${runId} is ${state.status}`);
    }
    if (state.status === "gated") {
      throw new RpcError(RPC_INVALID_PARAMS, `resume: run ${runId} is gated, answer it instead`);
    }
    if (state.status === "running") return { run_id: state.run_id, status: "running" };
    const reset = resetRunning(runDir);
    appendEvent(runDir, { run_id: reset.run_id, type: "warn", message: "run resumed" });
    rt.log(`resume: run ${runId}`);
    return { run_id: reset.run_id, status: reset.status };
  };

  return {
    preflight: notImplemented("preflight"),
    run: notImplemented("run"),
    wait,
    answer: notImplemented("answer"),
    status,
    cancel,
    resume,
    report: notImplemented("report"),
    nudge: notImplemented("nudge"),
    child_report: notImplemented("child_report"),
    child_ask: notImplemented("child_ask"),
    child_context: notImplemented("child_context"),
    child_checkpoint: notImplemented("child_checkpoint"),
  };
}

// ---- server -----------------------------------------------------------------------------------------------

export type CloseReason = "idle" | "shutdown" | "closed" | "signal";

export type DaemonOptions = DaemonPathOpts & {
  /** Exit after this long with no active runs and no clients. Default 30 min. */
  idleMs?: number;
  version?: string;
  handlers?: HandlerSource;
  /** Extra liveness signal for the executor (queued children, in-flight adapters). */
  isBusy?: () => boolean;
  log?: (line: string) => void;
  wait?: WaitTuning;
};

export type Daemon = {
  runtime: DaemonRuntime;
  handlers: DaemonHandlers;
  connections: () => number;
  activeRuns: () => number;
  close: (reason?: CloseReason) => Promise<void>;
  closed: Promise<CloseReason>;
};

function listenUnix(server: Server, path: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const onError = (err: Error): void => reject(err);
    server.once("error", onError);
    server.listen(path, () => {
      server.off("error", onError);
      resolve();
    });
  });
}

/** Start the daemon in this process. Throws `DaemonError("ALREADY_RUNNING")` when another owns the lock. */
export async function startDaemon(opts: DaemonOptions = {}): Promise<Daemon> {
  const paths = daemonPaths(opts);
  const log = opts.log ?? (() => {});
  mkdirSync(paths.dataRoot, { recursive: true, mode: 0o700 });
  mkdirSync(dirname(paths.socketPath), { recursive: true, mode: 0o700 });
  // Synchronous, before any await: concurrent in-process starts serialize here.
  acquireLock(paths.lockPath);

  const version = opts.version ?? buildId();
  const started_at = utcNow();
  const idleMs = opts.idleMs ?? IDLE_MS_DEFAULT;

  const runtime: DaemonRuntime = {
    paths,
    version,
    pid: process.pid,
    started_at,
    log,
    listRunDirs: () => listRunDirs(paths.runsRoot),
    findRunDir: (runId) => findRunDir(paths.runsRoot, runId),
    requireRunDir: (runId) => {
      const dir = findRunDir(paths.runsRoot, runId);
      if (!dir) throw domainError("RUN_NOT_FOUND", `no such run: ${runId}`, { run_id: runId });
      return dir;
    },
  };

  const activeRuns = (): number => {
    let n = 0;
    for (const dir of listRunDirs(paths.runsRoot)) {
      const s = readStateSafe(dir);
      if (s && ACTIVE_RUN.has(s.status)) n++;
    }
    return n;
  };

  let server: Server;
  try {
    recoverRuns(paths.runsRoot, log);
    const injected =
      typeof opts.handlers === "function" ? opts.handlers(runtime) : (opts.handlers ?? {});
    const handlers: DaemonHandlers = { ...ledgerHandlers(runtime, opts.wait ?? {}), ...injected };

    rmSync(paths.socketPath, { force: true });
    server = createServer();
    await listenUnix(server, paths.socketPath);
    chmodSync(paths.socketPath, 0o600);

    const sockets = new Set<Socket>();
    let lastBusy = Date.now();
    let shutdownRequested = false;
    let closing: Promise<void> | null = null;
    const { promise: closed, resolve: resolveClosed } = Promise.withResolvers<CloseReason>();

    const close = (reason: CloseReason = "closed"): Promise<void> => {
      if (closing) return closing;
      closing = (async () => {
        clearInterval(ticker);
        for (const s of sockets) s.destroy();
        await new Promise<void>((resolve) => server.close(() => resolve()));
        rmSync(paths.socketPath, { force: true });
        releaseLock(paths.lockPath);
        log(`engined: closed (${reason})`);
        resolveClosed(reason);
      })();
      return closing;
    };

    const tick = Math.max(25, Math.min(Math.floor(idleMs / 4), 5000));
    const ticker = setInterval(() => {
      const runs = activeRuns();
      if (shutdownRequested && runs === 0) {
        void close("shutdown");
        return;
      }
      if (sockets.size > 0 || runs > 0 || opts.isBusy?.()) {
        lastBusy = Date.now();
        return;
      }
      if (Date.now() - lastBusy >= idleMs) void close("idle");
    }, tick);

    const hello = (params: unknown): HelloResult => {
      const rec = asRecord(params, "hello") as Partial<HelloParams>;
      if (typeof rec.version !== "string" || typeof rec.client !== "string") {
        throw new RpcError(RPC_INVALID_PARAMS, "hello: version and client are required");
      }
      if (rec.version !== version) {
        throw domainError(
          "DAEMON_VERSION_MISMATCH",
          `daemon ${version} does not match client ${rec.version}`,
          { daemon_version: version, client_version: rec.version, pid: process.pid },
        );
      }
      return { version, pid: process.pid, started_at };
    };

    const shutdown = (params: unknown): ShutdownResult => {
      const rec = (params === undefined ? {} : asRecord(params, "shutdown")) as ShutdownParams;
      const runs = activeRuns();
      shutdownRequested = true;
      if (rec.when === "now" || runs === 0) setImmediate(() => void close("shutdown"));
      return { accepted: true, active_runs: runs };
    };

    server.on("connection", (socket: Socket) => {
      sockets.add(socket);
      lastBusy = Date.now();
      let greeted = false;
      const map: RpcHandlerMap = {
        hello: (p) => {
          const res = hello(p);
          greeted = true;
          return res;
        },
        shutdown,
      };
      for (const name of Object.keys(handlers) as (keyof DaemonHandlers)[]) {
        map[name] = (p, ctx) => (handlers[name] as Handler<MethodName>)(p as never, ctx);
      }
      serveConnection(socket, map, {
        guard: (method) => {
          if (!greeted && method !== "hello" && method !== "shutdown") {
            throw new RpcError(RPC_INVALID_REQUEST, `hello required before ${method}`);
          }
        },
        onClose: () => {
          sockets.delete(socket);
          lastBusy = Date.now();
        },
        onError: (err) => log(`engined: connection error ${err.message}`),
      });
    });

    log(`engined: listening ${paths.socketPath} (pid ${process.pid}, v${version})`);
    return {
      runtime,
      handlers,
      connections: () => sockets.size,
      activeRuns,
      close,
      closed,
    };
  } catch (err) {
    releaseLock(paths.lockPath);
    throw err;
  }
}

// ---- CLI: `wise-engine daemon <serve|start|stop|status>` -----------------------------------------------------

export type DaemonIo = { out: (s: string) => void; err: (s: string) => void; env?: Env };

const DAEMON_USAGE = `wise-engine daemon <serve|start|stop|status> [options]

  serve    run in the foreground (what the detached start launches)
  start    start a detached daemon if none answers; prints its status
  stop     ask the daemon to exit when idle (--now: exit immediately)
  status   socket alive, pid, version

Options: --data-root <dir> --socket <path> --lock <path> --log <path> --idle-ms <n> --json
`;

type DaemonArgs = { sub: string; flags: Record<string, string | true> };

function parseDaemonArgs(argv: readonly string[]): DaemonArgs {
  const [sub = "help", ...rest] = argv;
  const flags: Record<string, string | true> = {};
  for (let i = 0; i < rest.length; i++) {
    const tok = rest[i] as string;
    if (!tok.startsWith("--")) continue;
    const eq = tok.indexOf("=");
    if (eq > 0) {
      flags[tok.slice(2, eq)] = tok.slice(eq + 1);
      continue;
    }
    const next = rest[i + 1];
    if (next !== undefined && !next.startsWith("--")) {
      flags[tok.slice(2)] = next;
      i++;
    } else {
      flags[tok.slice(2)] = true;
    }
  }
  return { sub, flags };
}

function pathOptsFrom(args: DaemonArgs, env: Env): DaemonPathOpts & { idleMs?: number } {
  const opts: DaemonPathOpts & { idleMs?: number } = { env };
  const s = (k: string): string | undefined =>
    typeof args.flags[k] === "string" ? (args.flags[k] as string) : undefined;
  const dataRoot = s("data-root");
  const socket = s("socket");
  const lock = s("lock");
  const logPath = s("log");
  const idle = s("idle-ms");
  if (dataRoot) opts.dataRoot = dataRoot;
  if (socket) opts.socketPath = socket;
  if (lock) opts.lockPath = lock;
  if (logPath) opts.logPath = logPath;
  if (idle && /^\d+$/.test(idle)) opts.idleMs = Number(idle);
  return opts;
}

export async function daemonCommand(argv: string[], io: DaemonIo): Promise<number> {
  const args = parseDaemonArgs(argv);
  const env = io.env ?? process.env;
  const opts = pathOptsFrom(args, env);
  const json = args.flags.json === true;
  try {
    switch (args.sub) {
      case "serve": {
        // The executor (M2.3) is the default handler set; tests inject their own through `handlers`.
        const { executorHandlers } = await import("./executor.ts");
        let executor: Executor | undefined;
        let daemon: Daemon;
        try {
          daemon = await startDaemon({
            ...opts,
            log: (l) => io.out(`${utcNow()} ${l}\n`),
            handlers: executorHandlers({ env }, (e) => {
              executor = e;
            }),
            isBusy: () => executor?.isBusy() ?? false,
          });
        } catch (err) {
          if (err instanceof DaemonError && err.code === "ALREADY_RUNNING") {
            io.err(`engined: ${err.message}\n`);
            return 75;
          }
          throw err;
        }
        const onSignal = (): void => void daemon.close("signal");
        process.on("SIGTERM", onSignal);
        process.on("SIGINT", onSignal);
        executor?.pickUp();
        const reason = await daemon.closed;
        process.off("SIGTERM", onSignal);
        process.off("SIGINT", onSignal);
        executor?.stop();
        return reason === "signal" ? 130 : 0;
      }
      case "start": {
        const { ensureDaemon } = await import("./client.ts");
        const client = await ensureDaemon(opts);
        const hello = client.hello;
        client.close();
        io.out(
          json
            ? JSON.stringify({ alive: true, ...hello, socket: client.socketPath }) + "\n"
            : `engined running: pid ${hello.pid}, v${hello.version}, ${client.socketPath}\n`,
        );
        return 0;
      }
      case "stop": {
        const { stopDaemon } = await import("./client.ts");
        const res = await stopDaemon({ ...opts, now: args.flags.now === true });
        io.out(
          json
            ? JSON.stringify(res) + "\n"
            : res.was_running
              ? res.stopped
                ? "engined stopped\n"
                : `engined shutdown requested, ${res.active_runs} active run(s) keep it alive\n`
              : "engined not running\n",
        );
        return res.stopped ? 0 : 1;
      }
      case "status": {
        const { daemonStatus } = await import("./client.ts");
        const st = await daemonStatus(opts);
        io.out(
          json
            ? JSON.stringify(st) + "\n"
            : st.alive
              ? `engined running: pid ${st.pid}, v${st.version}${st.version_mismatch ? " (version mismatch)" : ""}, ${st.socketPath}\n`
              : `engined not running (${st.socketPath})\n`,
        );
        return st.alive ? 0 : 1;
      }
      case "help":
      case "--help":
      case "-h":
        io.out(DAEMON_USAGE);
        return 0;
      default:
        io.err(`wise-engine daemon: unknown subcommand '${args.sub}'\n\n${DAEMON_USAGE}`);
        return 64;
    }
  } catch (err) {
    io.err(`wise-engine daemon: ${(err as Error).message}\n`);
    return 70;
  }
}

if (import.meta.main ?? process.argv[1] === new URL(import.meta.url).pathname) {
  daemonCommand(process.argv.slice(2), {
    out: (s) => process.stdout.write(s),
    err: (s) => process.stderr.write(s),
  }).then((code) => {
    process.exitCode = code;
  });
}
