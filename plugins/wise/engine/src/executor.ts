// Run executor (plan M2.3): the daemon's `preflight` / `run` / `answer` / `resume` / `cancel` /
// `report` handlers plus the wave loop that dispatches agent, bash and gate steps under the P5
// concurrency caps and rate-limit backoff (E12). The ledger is the only state between ticks;
// the in-memory `LiveRun` holds what cannot live on disk: child handles, step tokens, fallbacks.

import { randomBytes } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join } from "node:path";
import { adapterFor, claudeAdapter, hasAdapter, startClaude } from "./adapters/index.ts";
import { collectNeeds, LOGIN_CMDS, probeHarnesses } from "./auth.ts";
import {
  answerFromDecisions,
  clipReportData,
  clipReportText,
  createChildTracker,
  progressLine,
  realTimers,
  resolveContextKey,
  STALE_AFTER_SECS_DEFAULT,
  staleNudgeText,
  startStaleWatch,
  writeCheckpoint,
} from "./channel.ts";
import type { ChannelTimers, ChildTracker, StaleWatch } from "./channel.ts";
import { clearChild, ledgerHandlers, readChild, recordChild } from "./daemon.ts";
import type { DaemonHandlers, DaemonRuntime, Handler } from "./daemon.ts";
import { defaultRoots, loadAndValidate, locateDef } from "./defs.ts";
import type { DefRoots } from "./defs.ts";
import {
  appendEvent,
  cwdSlug,
  initState,
  newUlid,
  readState,
  startRun,
  startStep,
  updateRun,
  updateStep,
  utcNow,
  writeState,
} from "./ledger.ts";
import type { EventInput } from "./ledger.ts";
import type { Env } from "./paths.ts";
import { applyAnswers, buildQuestionary, resolveFromContext } from "./preflight.ts";
import { isProfileLevel } from "./profile.ts";
import { RPC_INVALID_PARAMS, WAIT_DEFAULT_MS, WAIT_MAX_MS, WAIT_PROGRESS_MS } from "./protocol.ts";
import type { ChildAskResult, ProgressParams, ReportResult } from "./protocol.ts";
import { renderStep } from "./render.ts";
import { resolveModelDict } from "./resolve.ts";
import { domainError, RpcError } from "./rpc.ts";
import { nextWave } from "./scheduler.ts";
import { headline, startAgentStep } from "./steps/agent.ts";
import type { AgentHandle, AgentOutcome, AgentStarter, ChannelConfig } from "./steps/agent.ts";
import { startBashStep } from "./steps/bash.ts";
import { buildGate, decideGate, isGateStep } from "./steps/gate.ts";
import type { GateStep } from "./steps/gate.ts";
import { HARNESSES, REPORT_KINDS } from "./types.ts";
import type {
  Adapter,
  AgentStep,
  Answers,
  AskStep,
  BashStep,
  Context,
  Gate,
  Harness,
  LocatedDef,
  ProfileLevel,
  Project,
  ReportKind,
  Resolved,
  RunSummary,
  State,
  Usage,
  WorkflowDef,
} from "./types.ts";
import { ENGINE_ROOT } from "./version.ts";

// ---- configuration -----------------------------------------------------------------------------

export type ConcurrencyCaps = { global: number; harness: Record<Harness, number> };

/** P5 defaults: two Claude children, one per other harness, four in total. */
export const DEFAULT_CAPS: ConcurrencyCaps = {
  global: 4,
  harness: { claude: 2, codex: 1, gemini: 1, grok: 1 },
};

export type ConcurrencyOverrides = { global?: number; harness?: Partial<Record<Harness, number>> };

/** `$XDG_CONFIG_HOME/wise/engine.json`, else `~/.config/wise/engine.json`. */
export function defaultConfigPath(env: Env = process.env): string {
  const base = env.XDG_CONFIG_HOME || join(env.HOME ?? homedir(), ".config");
  return join(base, "wise", "engine.json");
}

function posInt(v: unknown): number | undefined {
  return typeof v === "number" && Number.isInteger(v) && v >= 1 ? v : undefined;
}

/**
 * Caps from `engine.json` (`{"concurrency": {"global": 4, "claude": 2, ...}}`), then explicit
 * overrides on top. A missing or malformed file yields the defaults.
 */
export function loadCaps(
  configPath: string,
  overrides: ConcurrencyOverrides = {},
): ConcurrencyCaps {
  const caps: ConcurrencyCaps = {
    global: DEFAULT_CAPS.global,
    harness: { ...DEFAULT_CAPS.harness },
  };
  try {
    const parsed = JSON.parse(readFileSync(configPath, "utf8")) as {
      concurrency?: Record<string, unknown>;
    };
    const conc = parsed?.concurrency ?? {};
    const global = posInt(conc.global);
    if (global !== undefined) caps.global = global;
    for (const h of HARNESSES) {
      const n = posInt(conc[h]);
      if (n !== undefined) caps.harness[h] = n;
    }
  } catch {
    // No config: defaults.
  }
  if (overrides.global !== undefined) caps.global = overrides.global;
  for (const h of HARNESSES) {
    const n = overrides.harness?.[h];
    if (n !== undefined) caps.harness[h] = n;
  }
  return caps;
}

/** E12 backoff: 1, 2, 4, 8 ... minutes, capped at 30. */
export function defaultBackoffMs(attempt: number): number {
  const minutes = Math.min(2 ** Math.max(0, attempt - 1), 30);
  return minutes * 60_000;
}

/** Small project heuristic for `{{project.kind}}`: manifest presence, nothing deeper. */
export function detectProject(cwd: string): Project {
  const has = (f: string): boolean => existsSync(join(cwd, f));
  let kind = "other";
  if (has("package.json")) kind = "node";
  else if (has("pyproject.toml") || has("requirements.txt")) kind = "python";
  else if (has("go.mod")) kind = "go";
  else if (has("Cargo.toml")) kind = "rust";
  return { path: cwd, name: basename(cwd), kind };
}

export type AdapterRegistry = Partial<Record<Harness, Adapter>>;

/** Child channel knobs (P8). Defaults: engine.sh under `ENGINE_ROOT`, the daemon's own paths. */
export type ChannelOptions = {
  engineRoot?: string;
  socketPath?: string;
  dataRoot?: string;
  /** `false` starts children without the engine MCP server (no daemon socket to reach). */
  inject?: boolean;
  /** Idle window before the stale policy acts when the step sets no `stale_after`; default 600. */
  staleAfterSecs?: number;
  /** `step.progress` throttle per step; default 30 000. */
  progressThrottleMs?: number;
  timers?: ChannelTimers;
};

export type ExecutorOptions = {
  env?: Env;
  /** Definition roots; default `defaultRoots({ env })`. */
  roots?: DefRoots;
  /** Adapter table; default: the real registry (`adapters/index.ts`). */
  adapters?: AdapterRegistry;
  /** How an agent child is started; default derives from `adapters` (Claude via `startClaude`). */
  startAgent?: AgentStarter;
  configPath?: string;
  concurrency?: ConcurrencyOverrides;
  backoffMs?: (attempt: number) => number;
  detectProject?: (cwd: string) => Project;
  /** Agent / bash wall clock when the step has no `timeout`; default 30 min. */
  defaultTimeoutMs?: number;
  channel?: ChannelOptions;
};

export type Executor = {
  handlers: DaemonHandlers;
  /** Children in flight or a rate-limit timer armed (daemon `isBusy`). */
  isBusy: () => boolean;
  /** Continue every `running` run in the ledger (clean daemon restart). */
  pickUp: () => string[];
  /** Kill children, drop timers, forget live runs. The ledger is left as is. */
  stop: () => void;
  /** Token of a live step for the child channel (M2.6); `undefined` once the step ended. */
  stepToken: (runId: string, stepId: string) => string | undefined;
  /** Resolve a presented token to its step. */
  stepByToken: (token: string) => { run_id: string; step: string } | undefined;
  /** Mid-run user message to a live Claude child (M2.6); `false` when it cannot be delivered. */
  nudge: (runId: string, stepId: string, text: string) => boolean;
  liveRuns: () => string[];
};

// ---- helpers --------------------------------------------------------------------------------------

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);

function asRecord(params: unknown, method: string): Rec {
  if (!isRec(params)) throw new RpcError(RPC_INVALID_PARAMS, `${method}: params must be an object`);
  return params;
}

function requireString(rec: Rec, key: string, method: string): string {
  const v = rec[key];
  if (typeof v !== "string" || v === "") {
    throw new RpcError(RPC_INVALID_PARAMS, `${method}: ${key} must be a non-empty string`);
  }
  return v;
}

function optionalRecord(rec: Rec, key: string, method: string): Rec {
  const v = rec[key];
  if (v === undefined || v === null) return {};
  if (!isRec(v)) throw new RpcError(RPC_INVALID_PARAMS, `${method}: ${key} must be an object`);
  return v;
}

/** `profile` param: the harness session's level, or undefined. Anything else is invalid params. */
function optionalProfile(rec: Rec, method: string): ProfileLevel | undefined {
  const v = rec.profile;
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "string" || !isProfileLevel(v)) {
    throw new RpcError(RPC_INVALID_PARAMS, `${method}: profile must be low | medium | max`);
  }
  return v;
}

function addUsage(into: Usage, u: Usage): void {
  into.input += u.input;
  into.output += u.output;
  into.cache_read += u.cache_read;
  into.cache_write += u.cache_write;
  if (u.cost_usd !== undefined) into.cost_usd = (into.cost_usd ?? 0) + u.cost_usd;
}

/** Primitive outputs only, strings clipped, for the compact `step.done` event (E1). */
function eventOutputs(
  outputs: Record<string, unknown>,
): Record<string, string | number | boolean> | undefined {
  const out: Record<string, string | number | boolean> = {};
  for (const [k, v] of Object.entries(outputs)) {
    if (typeof v === "string") out[k] = headline(v);
    else if (typeof v === "number" || typeof v === "boolean") out[k] = v;
  }
  return Object.keys(out).length ? out : undefined;
}

function randomToken(): string {
  return randomBytes(16).toString("hex");
}

type ControlMode = "synchronous" | "interactive";

function controlModeOf(def: WorkflowDef, answers: Answers): ControlMode {
  const answered = answers["control-mode"];
  if (answered === "synchronous" || answered === "interactive") return answered;
  return def.preflight?.["control-mode"] ?? "interactive";
}

// ---- live run --------------------------------------------------------------------------------------

type Child = {
  kill?: (signal?: NodeJS.Signals) => void;
  nudge?: (text: string) => void;
  pid?: number;
};

/** One `wise_ask` in flight: queued, gated, answered, or dropped with its child. */
type PendingAsk = {
  ask_id: string;
  step: string;
  question: string;
  options?: string[];
  allow_text?: boolean;
  gate_id?: string;
  value?: string;
  dropped: boolean;
  waiters: Set<() => void>;
};

type LiveRun = {
  runId: string;
  runDir: string;
  def: WorkflowDef;
  workflowDir: string;
  controlMode: ControlMode;
  stopped: boolean;
  children: Map<string, Child>;
  tokens: Map<string, string>;
  /** Harness a step moved to after a rate limit (E12). */
  harnessOverride: Map<string, Harness>;
  triedHarnesses: Map<string, Set<Harness>>;
  /** Live status per running agent child (P8). */
  trackers: Map<string, ChildTracker>;
  staleWatches: Map<string, StaleWatch>;
  /** Steps whose child the stale policy killed; their exit settles as `stale`. */
  staleKilled: Set<string>;
  asks: Map<string, PendingAsk>;
};

type Park = { until: number; attempts: number; timer: NodeJS.Timeout };

function emit(live: LiveRun, ev: Omit<EventInput, "run_id">): void {
  appendEvent(live.runDir, { run_id: live.runId, ...ev });
}

function makeLive(
  runId: string,
  runDir: string,
  def: WorkflowDef,
  workflowDir: string,
  controlMode: ControlMode,
): LiveRun {
  return {
    runId,
    runDir,
    def,
    workflowDir,
    controlMode,
    stopped: false,
    children: new Map(),
    tokens: new Map(),
    harnessOverride: new Map(),
    triedHarnesses: new Map(),
    trackers: new Map(),
    staleWatches: new Map(),
    staleKilled: new Set(),
    asks: new Map(),
  };
}

function dropAsk(ask: PendingAsk): void {
  ask.dropped = true;
  for (const w of ask.waiters) w();
}

function pendingAskFor(live: LiveRun, stepId: string): boolean {
  for (const a of live.asks.values()) {
    if (a.step === stepId && a.value === undefined && !a.dropped) return true;
  }
  return false;
}

function idleMinutes(ms: number): number {
  return Math.max(1, Math.round(ms / 60_000));
}

function optionalStringList(rec: Rec, key: string, method: string): string[] | undefined {
  const v = rec[key];
  if (v === undefined || v === null) return undefined;
  if (!Array.isArray(v) || !v.every((x) => typeof x === "string")) {
    throw new RpcError(RPC_INVALID_PARAMS, `${method}: ${key} must be a string array`);
  }
  return v as string[];
}

function clampWait(rec: Rec, method: string): number {
  const v = rec.timeout_ms;
  if (v === undefined || v === null) return WAIT_DEFAULT_MS;
  if (typeof v !== "number" || !Number.isFinite(v)) {
    throw new RpcError(RPC_INVALID_PARAMS, `${method}: timeout_ms must be a number`);
  }
  return Math.min(Math.max(0, v), WAIT_MAX_MS);
}

/** Block until the ask has a value, is dropped, the bound passes, or the connection goes away. */
async function awaitAsk(
  live: LiveRun,
  ask: PendingAsk,
  timeoutMs: number,
  ctx: { notify: (method: string, params: unknown) => void; signal: AbortSignal },
): Promise<void> {
  if (ask.value !== undefined || ask.dropped || timeoutMs <= 0) return;
  const start = Date.now();
  const { promise, resolve } = Promise.withResolvers<void>();
  const waiter = (): void => resolve();
  ask.waiters.add(waiter);
  const timer = setTimeout(resolve, timeoutMs);
  const ticker = setInterval(() => {
    const p: ProgressParams = { run_id: live.runId, waiting_ms: Date.now() - start };
    ctx.notify("progress", p);
  }, WAIT_PROGRESS_MS);
  ctx.signal.addEventListener("abort", waiter, { once: true });
  try {
    await promise;
  } finally {
    ask.waiters.delete(waiter);
    clearTimeout(timer);
    clearInterval(ticker);
    ctx.signal.removeEventListener("abort", waiter);
  }
}

/** Load and validate a located definition; both failure modes are `WORKFLOW_INVALID`. */
function validated(located: LocatedDef): WorkflowDef {
  let result: ReturnType<typeof loadAndValidate>;
  try {
    result = loadAndValidate(located);
  } catch (err) {
    throw domainError("WORKFLOW_INVALID", `${located.path}: ${(err as Error).message}`, {
      workflow: located.name,
      issues: [{ level: "error", path: "", message: (err as Error).message }],
    });
  }
  if (!result.def) {
    throw domainError("WORKFLOW_INVALID", `${located.path}: definition has errors`, {
      workflow: located.name,
      issues: result.issues,
    });
  }
  return result.def;
}

/** The run's planned resolution for a step, with any E12 fallback harness applied. */
function plannedResolution(live: LiveRun, state: State, step: AgentStep): Resolved {
  const planned = state.resolved[step.id] ?? {
    harness: step.harness ?? "claude",
    model: step.model ?? "inherit",
    effort: step.effort ?? "",
  };
  const override = live.harnessOverride.get(step.id);
  return override ? { ...planned, harness: override } : planned;
}

// ---- executor ----------------------------------------------------------------------------------------

export function createExecutor(rt: DaemonRuntime, opts: ExecutorOptions = {}): Executor {
  const env = opts.env ?? process.env;
  const roots = opts.roots ?? defaultRoots({ env });
  const caps = loadCaps(opts.configPath ?? defaultConfigPath(env), opts.concurrency);
  const backoffMs = opts.backoffMs ?? defaultBackoffMs;
  const projectOf = opts.detectProject ?? detectProject;
  const ledger = ledgerHandlers(rt);
  const timers = opts.channel?.timers ?? realTimers;
  const channel: ChannelConfig | undefined =
    opts.channel?.inject === false
      ? undefined
      : {
          engineRoot: opts.channel?.engineRoot ?? ENGINE_ROOT,
          socketPath: opts.channel?.socketPath ?? rt.paths.socketPath,
          dataRoot: opts.channel?.dataRoot ?? rt.paths.dataRoot,
        };

  const getAdapter = (h: Harness): Adapter | undefined =>
    opts.adapters ? opts.adapters[h] : hasAdapter(h) ? adapterFor(h) : undefined;

  const starter: AgentStarter =
    opts.startAgent ??
    ((harness, req, onEvent) => {
      const adapter = getAdapter(harness);
      if (!adapter) {
        throw domainError("HARNESS_UNAVAILABLE", `no adapter for harness "${harness}"`, {
          harness,
        });
      }
      // The real Claude adapter exposes pid and stdin through `startClaude`; fakes and other
      // harnesses only give a result promise.
      if (adapter === claudeAdapter) return startClaude(req, onEvent);
      return { done: adapter.run(req, onEvent) };
    });

  const lives = new Map<string, LiveRun>();
  const inFlight: Record<Harness, number> = { claude: 0, codex: 0, gemini: 0, grok: 0 };
  let inFlightGlobal = 0;
  const parked = new Map<Harness, Park>();

  // ---- definitions ------------------------------------------------------------------------------

  const locate = (ref: string): LocatedDef => {
    if (isAbsolute(ref) && (ref.endsWith(".yaml") || ref.endsWith(".yml"))) {
      let ok = false;
      try {
        ok = statSync(ref).isFile();
      } catch {
        ok = false;
      }
      if (!ok)
        throw domainError("WORKFLOW_NOT_FOUND", `no workflow file at ${ref}`, { workflow: ref });
      const dir = dirname(ref);
      const name =
        basename(ref) === "workflow.yaml" ? basename(dir) : basename(ref).replace(/\.ya?ml$/, "");
      return { name, path: ref, dir, source: "user" };
    }
    const located = locateDef(ref, roots);
    if (!located) {
      throw domainError("WORKFLOW_NOT_FOUND", `no workflow named ${JSON.stringify(ref)}`, {
        workflow: ref,
      });
    }
    return located;
  };

  /** Reload a run's definition by name, then by its recorded directory (path-form runs). */
  const reloadDef = (wf: State["workflow"]): { def: WorkflowDef; dir: string } => {
    const byName = locateDef(wf.name, roots);
    if (byName) return { def: validated(byName), dir: byName.dir };
    if (wf.dir) {
      for (const candidate of [join(wf.dir, "workflow.yaml"), join(wf.dir, `${wf.name}.yaml`)]) {
        if (existsSync(candidate)) {
          return { def: validated(locate(candidate)), dir: wf.dir };
        }
      }
    }
    throw domainError("WORKFLOW_NOT_FOUND", `workflow ${JSON.stringify(wf.name)} is gone`, {
      workflow: wf.name,
    });
  };

  const ensureLive = (runDir: string, state: State): LiveRun => {
    const existing = lives.get(state.run_id);
    if (existing) return existing;
    const { def, dir } = reloadDef(state.workflow);
    const live = makeLive(state.run_id, runDir, def, dir, controlModeOf(def, state.answers));
    lives.set(state.run_id, live);
    return live;
  };

  // ---- scheduling -----------------------------------------------------------------------------------

  const harnessFree = (h: Harness): boolean => {
    const park = parked.get(h);
    if (park && Date.now() < park.until) return false;
    return inFlight[h] < (caps.harness[h] ?? 1) && inFlightGlobal < caps.global;
  };

  const scheduleAll = (): void => {
    for (const live of lives.values()) schedule(live);
  };

  /** One pass over the DAG: skips, completion, dispatch, at most one gate. Sync, re-entrant safe. */
  function schedule(live: LiveRun): void {
    for (;;) {
      if (live.stopped) return;
      const state = readState(live.runDir);
      if (state.status !== "running") return;
      const wave = nextWave(live.def, state);
      for (const w of wave.warnings) emit(live, { type: "warn", message: w });
      if (wave.skipped.length > 0) {
        const now = utcNow();
        for (const s of wave.skipped) {
          updateStep(live.runDir, s.id, {
            status: "skipped",
            verdict: headline(`skipped: ${s.reason}`),
            completed_at: now,
          });
          emit(live, { type: "step.done", step: s.id, verdict: headline(`skipped: ${s.reason}`) });
        }
        continue;
      }
      if (wave.done) {
        finish(live, wave.failed);
        return;
      }
      let gate: GateStep | undefined;
      for (const step of wave.ready) {
        if (isGateStep(step)) {
          gate ??= step;
          continue;
        }
        if (step.type === "bash") {
          dispatchBash(live, state, step);
        } else if (step.type === "agent") {
          const harness = plannedResolution(live, state, step).harness;
          if (harnessFree(harness)) dispatchAgent(live, state, step);
        } else {
          updateStep(live.runDir, step.id, {
            status: "failed",
            error: "units steps arrive in M4",
            verdict: "failed: units steps arrive in M4",
            completed_at: utcNow(),
          });
          emit(live, {
            type: "step.done",
            step: step.id,
            verdict: "failed: units steps arrive in M4",
          });
          continue;
        }
      }
      if (gate) {
        const opened = openGate(live, gate);
        if (!opened) continue; // auto-approved: the DAG moved, take another pass
      }
      return;
    }
  }

  function finish(live: LiveRun, failed: boolean): void {
    const state = readState(live.runDir);
    const now = utcNow();
    if (failed) {
      if (!state.error) {
        const bad = Object.entries(state.steps).find(([, s]) => s.status === "failed");
        state.error = bad
          ? `${bad[0]}: ${bad[1].error ?? bad[1].verdict ?? "failed"}`
          : "unreachable steps";
      }
      state.status = "failed";
    } else {
      state.status = "completed";
    }
    state.completed_at = now;
    state.last_activity_at = now;
    delete state.gate;
    writeState(live.runDir, state);
    emit(
      live,
      failed
        ? { type: "run.failed", verdict: headline(state.error ?? "failed") }
        : { type: "run.done", verdict: "completed" },
    );
    rt.log(`run ${live.runId}: ${state.status}${failed ? ` (${state.error})` : ""}`);
    dropLive(live);
  }

  function dropLive(live: LiveRun): void {
    live.stopped = true;
    for (const w of live.staleWatches.values()) w.stop();
    live.staleWatches.clear();
    for (const ask of live.asks.values()) dropAsk(ask);
    live.asks.clear();
    live.trackers.clear();
    live.children.clear();
    live.tokens.clear();
    lives.delete(live.runId);
  }

  // ---- child asks (P8) --------------------------------------------------------------------------------

  /** Open a gate for the oldest queued child question when the run is free to take one. */
  function openChildAsks(live: LiveRun): void {
    if (live.stopped) return;
    const state = readState(live.runDir);
    if (state.status !== "running" || state.gate) return;
    const next = [...live.asks.values()].find(
      (a) => a.gate_id === undefined && a.value === undefined && !a.dropped,
    );
    if (!next) return;
    const gate: Gate = {
      gate_id: newUlid(),
      step: next.step,
      kind: "ask",
      message: next.question.trim(),
    };
    if (next.options && next.options.length > 0) {
      gate.options = next.options.map((o) => ({ value: o, label: o }));
    }
    if (next.allow_text !== undefined) gate.allow_text = next.allow_text;
    next.gate_id = gate.gate_id;
    updateRun(live.runDir, { status: "gated", gate });
    emit(live, { type: "gate.opened", step: next.step, verdict: headline(gate.message) });
  }

  /** The child behind these questions is gone: drop them and reopen the run if one was gated. */
  function closeChildAsks(live: LiveRun, stepId: string): void {
    let closedGate = false;
    for (const [id, ask] of live.asks) {
      if (ask.step !== stepId) continue;
      live.asks.delete(id);
      dropAsk(ask);
      if (ask.gate_id === undefined) continue;
      const state = readState(live.runDir);
      if (state.gate?.gate_id !== ask.gate_id) continue;
      state.status = "running";
      state.last_activity_at = utcNow();
      delete state.gate;
      writeState(live.runDir, state);
      closedGate = true;
    }
    if (closedGate) {
      emit(live, {
        type: "warn",
        step: stepId,
        message: `${stepId}: child exited with an open question; gate closed`,
      });
    }
    openChildAsks(live);
  }

  /** `answer` on a child-ask gate: hand the value to the blocked `wise_ask`, nudge the child. */
  function answerChildAsk(
    live: LiveRun,
    ask: PendingAsk,
    value: string | string[],
  ): { accepted: boolean } {
    const synthetic: AskStep = { id: ask.step, type: "ask", message: ask.question };
    if (ask.options !== undefined) synthetic.options = ask.options;
    if (ask.allow_text !== undefined) synthetic.allow_text = ask.allow_text;
    const decision = decideGate(synthetic, value);
    const text = decision.output?.value ?? "";
    const state = readState(live.runDir);
    state.status = "running";
    state.last_activity_at = utcNow();
    delete state.gate;
    writeState(live.runDir, state);
    ask.value = text;
    for (const w of ask.waiters) w();
    emit(live, { type: "gate.answered", step: ask.step, verdict: headline(`answered: ${text}`) });
    const child = live.children.get(ask.step);
    if (child?.nudge) {
      try {
        child.nudge(`Answer to your question: ${text}`);
      } catch {
        // Stdin already closed: the tool result still carries the answer.
      }
    }
    live.trackers.get(ask.step)?.touch();
    openChildAsks(live);
    setImmediate(() => schedule(live));
    return { accepted: true };
  }

  // ---- gates ----------------------------------------------------------------------------------------

  /** Returns `true` when the run is now parked; `false` when the gate resolved itself. */
  function openGate(live: LiveRun, def: GateStep): boolean {
    startStep(live.runDir, def.id);
    const state = readState(live.runDir);
    const step = renderStep(def, state, live.workflowDir, live.runDir) as GateStep;
    if (step.type === "approval" && live.controlMode === "synchronous") {
      const verdict = "auto-approved (control-mode synchronous)";
      updateStep(live.runDir, step.id, { status: "completed", verdict, completed_at: utcNow() });
      emit(live, {
        type: "warn",
        step: step.id,
        message: `approval ${step.id} auto-approved: control-mode synchronous`,
      });
      emit(live, { type: "step.done", step: step.id, verdict });
      return false;
    }
    const gate = buildGate(step, newUlid());
    updateRun(live.runDir, { status: "gated", gate });
    emit(live, { type: "gate.opened", step: step.id, verdict: headline(gate.message) });
    return true;
  }

  // ---- dispatch ---------------------------------------------------------------------------------------

  function dispatchBash(live: LiveRun, state: State, def: BashStep): void {
    const stepRunId = startStep(live.runDir, def.id);
    const fresh = readState(live.runDir);
    const step = renderStep(def, fresh, live.workflowDir, live.runDir) as BashStep;
    emit(live, { type: "step.started", step: step.id });
    let handle: ReturnType<typeof startBashStep>;
    try {
      handle = startBashStep(step, {
        cwd: state.cwd,
        parentEnv: env,
        ...(opts.defaultTimeoutMs !== undefined ? { defaultTimeoutMs: opts.defaultTimeoutMs } : {}),
      });
    } catch (err) {
      failStep(live, step.id, (err as Error).message);
      return;
    }
    live.children.set(step.id, { kill: handle.kill, pid: handle.pid });
    void handle.result.then((res) => {
      live.children.delete(step.id);
      if (live.stopped || readState(live.runDir).steps[step.id]?.step_run_id !== stepRunId) return;
      if (res.ok) {
        completeStep(live, step.id, res.verdict, res.outputs);
      } else {
        failStep(live, step.id, res.error ?? "failed", res.verdict);
      }
      schedule(live);
    });
  }

  function dispatchAgent(live: LiveRun, state: State, def: AgentStep): void {
    const resolved = plannedResolution(live, state, def);
    const harness = resolved.harness;
    const stepRunId = startStep(live.runDir, def.id);
    updateStep(live.runDir, def.id, { resolved });
    const fresh = readState(live.runDir);
    const step = renderStep(def, fresh, live.workflowDir, live.runDir) as AgentStep;
    const started: EventInput = {
      run_id: live.runId,
      type: "step.started",
      step: step.id,
      harness,
      model: resolved.model,
    };
    if (resolved.effort !== "") started.effort = resolved.effort;
    appendEvent(live.runDir, started);
    const tried = live.triedHarnesses.get(step.id) ?? new Set<Harness>();
    tried.add(harness);
    live.triedHarnesses.set(step.id, tried);

    const token = randomToken();
    live.tokens.set(step.id, token);
    const cursor = fresh.steps[step.id]?.cursor;
    inFlight[harness] += 1;
    inFlightGlobal += 1;
    let release = (): void => {
      inFlight[harness] -= 1;
      inFlightGlobal -= 1;
      release = () => {};
    };

    const tracker = createChildTracker({
      step: step.id,
      now: timers.now,
      ...(opts.channel?.progressThrottleMs !== undefined
        ? { throttleMs: opts.channel.progressThrottleMs }
        : {}),
    });
    live.trackers.set(step.id, tracker);
    let run: { handle: AgentHandle; outcome: Promise<AgentOutcome> };
    try {
      run = startAgentStep({
        runDir: live.runDir,
        stepRunId,
        step,
        resolved,
        cwd: state.cwd,
        ...(cursor !== undefined ? { cursor } : {}),
        stepToken: token,
        starter,
        ...(opts.defaultTimeoutMs !== undefined ? { defaultTimeoutMs: opts.defaultTimeoutMs } : {}),
        ...(channel !== undefined ? { channel } : {}),
        onEvent: (e) => {
          const progress = tracker.ingest(e);
          if (progress && !live.stopped) {
            emit(live, { type: "step.progress", step: step.id, message: progressLine(progress) });
          }
        },
      });
    } catch (err) {
      release();
      live.tokens.delete(step.id);
      live.trackers.delete(step.id);
      failStep(live, step.id, (err as Error).message);
      return;
    }
    const child: Child = {};
    if (run.handle.pid !== undefined) child.pid = run.handle.pid;
    if (run.handle.kill) child.kill = run.handle.kill;
    if (run.handle.nudge) child.nudge = run.handle.nudge;
    live.children.set(step.id, child);
    if (run.handle.pid !== undefined && run.handle.pid > 0) {
      recordChild(live.runDir, { pgid: run.handle.pid, pid: run.handle.pid });
    }
    const staleSecs = step.stale_after ?? opts.channel?.staleAfterSecs ?? STALE_AFTER_SECS_DEFAULT;
    live.staleWatches.set(
      step.id,
      startStaleWatch({
        staleMs: staleSecs * 1000,
        timers,
        lastActivityMs: () => tracker.lastActivityMs(),
        paused: () => pendingAskFor(live, step.id),
        nudge: (idle) => {
          if (!child.nudge) return false;
          try {
            child.nudge(staleNudgeText(idle));
          } catch {
            return false;
          }
          emit(live, {
            type: "warn",
            step: step.id,
            message: `${step.id}: idle for ${idleMinutes(idle)} min, nudged`,
          });
          return true;
        },
        kill: () => {
          if (!child.kill) {
            emit(live, { type: "warn", step: step.id, message: `${step.id}: stale, cannot kill` });
            return;
          }
          live.staleKilled.add(step.id);
          emit(live, {
            type: "warn",
            step: step.id,
            message: `${step.id}: stale, killed${child.nudge ? "" : "; resume from cursor"}`,
          });
          child.kill("SIGTERM");
        },
      }),
    );
    void run.outcome
      .catch((err: unknown): AgentOutcome => ({
        exit: "error",
        ok: false,
        outputs: {},
        verdict: headline(`error: ${(err as Error).message}`),
        error: (err as Error).message,
        usage: {
          input: 0,
          output: 0,
          cache_read: 0,
          cache_write: 0,
          pool: step.auth ?? "subscription",
        },
        warnings: [],
      }))
      .then((outcome) => {
        release();
        live.children.delete(step.id);
        live.tokens.delete(step.id);
        live.trackers.delete(step.id);
        live.staleWatches.get(step.id)?.stop();
        live.staleWatches.delete(step.id);
        if (run.handle.pid !== undefined && readChild(live.runDir)?.pid === run.handle.pid) {
          clearChild(live.runDir);
        }
        if (live.stopped) {
          scheduleAll();
          return;
        }
        closeChildAsks(live, step.id);
        const current = readState(live.runDir).steps[step.id];
        if (current?.step_run_id !== stepRunId) {
          scheduleAll();
          return;
        }
        settleAgent(live, step, harness, resolved, outcome);
        scheduleAll();
      });
  }

  function settleAgent(
    live: LiveRun,
    step: AgentStep,
    harness: Harness,
    resolved: Resolved,
    outcome: AgentOutcome,
  ): void {
    foldUsage(live, step.id, harness, outcome.usage);
    for (const w of outcome.warnings)
      emit(live, { type: "warn", step: step.id, message: headline(w) });
    const cursorPatch = outcome.cursor !== undefined ? { cursor: outcome.cursor } : {};
    if (live.staleKilled.delete(step.id)) {
      // E8: the cursor is kept so a `resume: unit` step picks the session back up.
      failStep(live, step.id, "stale", "failed: stale (no activity, killed)", cursorPatch);
      return;
    }
    switch (outcome.exit) {
      case "ok":
        completeStep(live, step.id, outcome.verdict, outcome.outputs, {
          ...cursorPatch,
          usage: outcome.usage,
        });
        return;
      case "rate_limited":
        rateLimited(live, step, harness, outcome.error ?? "rate limited");
        return;
      case "auth":
        authFailed(live, step, harness, resolved, outcome.error ?? "not logged in");
        return;
      default:
        failStep(live, step.id, outcome.error ?? outcome.exit, outcome.verdict, cursorPatch);
    }
  }

  function foldUsage(live: LiveRun, stepId: string, harness: Harness, usage: Usage): void {
    const state = readState(live.runDir);
    addUsage(state.usage[usage.pool], usage);
    const byHarness = (state.usage.by_harness[harness] ??= {
      input: 0,
      output: 0,
      cache_read: 0,
      cache_write: 0,
      pool: usage.pool,
    });
    addUsage(byHarness, usage);
    state.last_activity_at = utcNow();
    writeState(live.runDir, state);
    emit(live, { type: "usage", step: stepId, harness, usage });
  }

  /** Step completed with its outputs merged into the run outputs, one atomic state write. */
  function completeStep(
    live: LiveRun,
    stepId: string,
    verdict: string,
    outputs: Record<string, unknown>,
    extra: { cursor?: unknown; usage?: Usage } = {},
  ): void {
    const state = readState(live.runDir);
    const step = state.steps[stepId];
    if (!step) return;
    const now = utcNow();
    step.status = "completed";
    step.completed_at = now;
    step.verdict = verdict;
    step.outputs = outputs;
    if (extra.cursor !== undefined) step.cursor = extra.cursor;
    delete step.error;
    state.outputs = { ...state.outputs, ...outputs };
    state.last_activity_at = now;
    writeState(live.runDir, state);
    const ev: Omit<EventInput, "run_id"> = { type: "step.done", step: stepId, verdict };
    const compact = eventOutputs(outputs);
    if (compact) ev.outputs = compact;
    if (extra.usage) ev.usage = extra.usage;
    emit(live, ev);
  }

  function failStep(
    live: LiveRun,
    stepId: string,
    error: string,
    verdict: string = headline(`failed: ${error}`),
    extra: { cursor?: unknown } = {},
  ): void {
    updateStep(live.runDir, stepId, {
      status: "failed",
      error,
      verdict,
      completed_at: utcNow(),
      ...(extra.cursor !== undefined ? { cursor: extra.cursor } : {}),
    });
    emit(live, { type: "step.done", step: stepId, verdict });
  }

  // ---- rate limits and auth (E12, P5) ------------------------------------------------------------------

  function rateLimited(live: LiveRun, step: AgentStep, harness: Harness, error: string): void {
    const previous = parked.get(harness);
    const attempts = (previous?.attempts ?? 0) + 1;
    const delay = backoffMs(attempts);
    if (previous) clearTimeout(previous.timer);
    const timer = setTimeout(() => {
      const park = parked.get(harness);
      if (park && park.timer === timer) parked.delete(harness);
      scheduleAll();
    }, delay);
    timer.unref?.();
    parked.set(harness, { until: Date.now() + delay, attempts, timer });
    // Back to pending: the next wave re-offers the step once the harness (or a fallback) is free.
    updateStep(live.runDir, step.id, { status: "pending", error: headline(error) });
    emit(live, {
      type: "warn",
      step: step.id,
      harness,
      message: headline(
        `rate limited on ${harness}; backoff ${Math.round(delay / 1000)}s: ${error}`,
      ),
    });
    const group = live.def.tuning?.groups.find((g) => g.id === step.group);
    const fallback = step.fallback ?? group?.fallback ?? [];
    const tried = live.triedHarnesses.get(step.id) ?? new Set<Harness>([harness]);
    const next = fallback.find((h) => !tried.has(h));
    if (next === undefined) return;
    if (getAdapter(next)) {
      live.harnessOverride.set(step.id, next);
      emit(live, {
        type: "warn",
        step: step.id,
        harness: next,
        message: `${step.id} falls back to ${next}`,
      });
    } else {
      emit(live, {
        type: "warn",
        step: step.id,
        message: `${step.id}: no adapter for fallback ${next}; waiting for ${harness}`,
      });
    }
  }

  function authFailed(
    live: LiveRun,
    step: AgentStep,
    harness: Harness,
    resolved: Resolved,
    error: string,
  ): void {
    const loginCmd = LOGIN_CMDS[harness];
    failStep(live, step.id, `AUTH_REQUIRED: ${error}`);
    // Other children of this run stop now; their steps go back to pending for a later resume.
    for (const [id, child] of live.children) {
      child.kill?.("SIGTERM");
      live.children.delete(id);
    }
    const state = readState(live.runDir);
    for (const [id, s] of Object.entries(state.steps)) {
      if (id !== step.id && s.status === "running") {
        s.status = "pending";
        delete s.started_at;
        delete s.step_run_id;
      }
    }
    const now = utcNow();
    state.status = "failed";
    state.error = `AUTH_REQUIRED: ${harness} (${resolved.model}) not logged in; run \`${loginCmd}\``;
    state.completed_at = now;
    state.last_activity_at = now;
    delete state.gate;
    writeState(live.runDir, state);
    emit(live, { type: "run.failed", harness, verdict: headline(state.error) });
    rt.log(`run ${live.runId}: failed (${state.error})`);
    dropLive(live);
  }

  // ---- handlers ---------------------------------------------------------------------------------------

  const preflight: Handler<"preflight"> = (params) => {
    const rec = asRecord(params, "preflight");
    const workflow = requireString(rec, "workflow", "preflight");
    requireString(rec, "cwd", "preflight");
    const profile = optionalProfile(rec, "preflight");
    const located = locate(workflow);
    const def = validated(located);
    const q = buildQuestionary(def, profile !== undefined ? { profile } : {});
    return {
      workflow: located.name,
      version: def.version,
      questions: q.questions,
      defaults: q.defaults,
    };
  };

  const run: Handler<"run"> = async (params) => {
    const rec = asRecord(params, "run");
    const workflow = requireString(rec, "workflow", "run");
    const cwd = requireString(rec, "cwd", "run");
    const answers = { ...(optionalRecord(rec, "answers", "run") as Answers) };
    const context = optionalRecord(rec, "context", "run") as Context;
    const explicitInputs = optionalRecord(rec, "inputs", "run") as Record<string, string>;
    // The session profile stands in for an unanswered `profile` question (the conductor skips it).
    const sessionProfile = optionalProfile(rec, "run");
    if (answers.profile === undefined && sessionProfile !== undefined)
      answers.profile = sessionProfile;

    const located = locate(workflow);
    const def = validated(located);
    const applied = applyAnswers(def, answers);
    const controlMode = controlModeOf(def, answers);

    const resolved: Record<string, Resolved> = {};
    for (const step of def.steps) {
      if (step.type !== "agent" || !applied.enabledSteps.has(step.id)) continue;
      const group = step.group !== undefined ? applied.tuning[step.group] : undefined;
      const harness: Harness = step.harness ?? group?.harness ?? "claude";
      const r = resolveModelDict(
        step.model ?? group?.model ?? "",
        step.effort ?? group?.effort ?? "",
        applied.profile,
        { harness, env },
      );
      const entry: Resolved = { harness: r.harness, model: r.model, effort: r.effort };
      if (r.reason !== undefined) entry.reason = r.reason;
      resolved[step.id] = entry;
    }

    // R1: nothing is created until every harness the run needs answers its auth probe.
    await probeHarnesses(collectNeeds(def, applied.enabledSteps, resolved), getAdapter);

    const runId = newUlid();
    const runDir = join(rt.paths.runsRoot, cwdSlug(cwd), runId);
    const state = initState({
      runDir,
      runId,
      workflow: { name: located.name, version: def.version, dir: located.dir },
      stepIds: def.steps.map((s) => s.id),
      cwd,
      profile: applied.profile,
    });
    const now = utcNow();
    for (const step of def.steps) {
      if (applied.enabledSteps.has(step.id)) continue;
      const st = state.steps[step.id];
      if (st) {
        st.status = "skipped";
        st.verdict = "skipped: deselected in pre-flight";
        st.completed_at = now;
      }
    }
    writeState(runDir, state);
    const inputs = { ...applied.inputs, ...explicitInputs };
    // E1: an input left empty by the harness still takes its `from-context` value from the run context.
    for (const input of def.inputs ?? []) {
      const path = input["from-context"];
      if (!path || inputs[input.name]) continue;
      const fromContext = resolveFromContext(path, context);
      if (fromContext !== undefined) inputs[input.name] = fromContext;
    }
    startRun(runDir, {
      project: projectOf(cwd),
      inputs,
      answers,
      context,
      profile: applied.profile,
      resolved,
      caps: applied.caps,
    });
    appendEvent(runDir, {
      run_id: runId,
      type: "run.started",
      verdict: headline(
        `${located.name} profile=${applied.profile} control=${controlMode} steps=${applied.enabledSteps.size}/${def.steps.length}`,
      ),
    });
    const live = makeLive(runId, runDir, def, located.dir, controlMode);
    lives.set(runId, live);
    rt.log(`run ${runId}: started ${located.name} in ${cwd}`);
    setImmediate(() => {
      try {
        schedule(live);
      } catch (err) {
        rt.log(`run ${runId}: scheduler error ${(err as Error).message}`);
      }
    });
    return { run_id: runId, status: "running" };
  };

  const answer: Handler<"answer"> = (params) => {
    const rec = asRecord(params, "answer");
    const runId = requireString(rec, "run_id", "answer");
    const gateId = requireString(rec, "gate_id", "answer");
    const raw = rec.value;
    if (
      typeof raw !== "string" &&
      !(Array.isArray(raw) && raw.every((v) => typeof v === "string"))
    ) {
      throw new RpcError(RPC_INVALID_PARAMS, "answer: value must be a string or string[]");
    }
    const value = raw as string | string[];
    const runDir = rt.requireRunDir(runId);
    const state = readState(runDir);
    if (state.status !== "gated" || !state.gate || state.gate.gate_id !== gateId) {
      throw domainError("GATE_STALE", `answer: run ${runId} has no open gate ${gateId}`, {
        run_id: runId,
        gate_id: gateId,
        ...(state.gate ? { current_gate_id: state.gate.gate_id } : {}),
      });
    }
    const live = ensureLive(runDir, state);
    const childAsk = [...live.asks.values()].find((a) => a.gate_id === gateId);
    if (childAsk) return answerChildAsk(live, childAsk, value);
    const def = live.def.steps.find((s) => s.id === state.gate?.step);
    if (!def || !isGateStep(def)) {
      throw domainError("GATE_STALE", `answer: gate step ${state.gate.step} is not a gate`, {
        run_id: runId,
        gate_id: gateId,
      });
    }
    const decision = decideGate(def, value);
    const fresh = readState(runDir);
    const step = fresh.steps[def.id];
    const now = utcNow();
    if (step) {
      step.status = decision.status;
      step.verdict = decision.verdict;
      step.completed_at = now;
      if (decision.status === "failed") step.error = decision.verdict;
      if (decision.output) {
        step.outputs = { [decision.output.name]: decision.output.value };
        fresh.outputs = { ...fresh.outputs, [decision.output.name]: decision.output.value };
      }
    }
    fresh.status = "running";
    fresh.last_activity_at = now;
    delete fresh.gate;
    writeState(runDir, fresh);
    const answered: Omit<EventInput, "run_id"> = {
      type: "gate.answered",
      step: def.id,
      verdict: decision.verdict,
    };
    if (decision.output)
      answered.outputs = { [decision.output.name]: headline(decision.output.value) };
    emit(live, answered);
    emit(live, { type: "step.done", step: def.id, verdict: decision.verdict });
    openChildAsks(live);
    setImmediate(() => schedule(live));
    return { accepted: true };
  };

  const resume: Handler<"resume"> = async (params, ctx) => {
    const res = await ledger.resume(params, ctx);
    if (res.status === "running") {
      const runDir = rt.requireRunDir(res.run_id);
      const live = ensureLive(runDir, readState(runDir));
      setImmediate(() => schedule(live));
    }
    return res;
  };

  const cancel: Handler<"cancel"> = (params, ctx) => {
    const rec = asRecord(params, "cancel");
    const runId = requireString(rec, "run_id", "cancel");
    const live = lives.get(runId);
    if (live) {
      for (const child of live.children.values()) child.kill?.("SIGTERM");
      dropLive(live);
    }
    return ledger.cancel(params, ctx);
  };

  const report: Handler<"report"> = (params) => {
    const rec = asRecord(params, "report");
    const runId = requireString(rec, "run_id", "report");
    const state = readState(rt.requireRunDir(runId));
    const verdicts: Record<string, string> = {};
    for (const [id, s] of Object.entries(state.steps)) {
      if (s.verdict !== undefined) verdicts[id] = s.verdict;
    }
    const out: ReportResult = { units: [], usage: state.usage, verdicts };
    return out;
  };

  // ---- child channel handlers (P8) ------------------------------------------------------------------

  const nudgeChild = (runId: string, stepId: string, text: string): boolean => {
    const child = lives.get(runId)?.children.get(stepId);
    if (!child?.nudge) return false;
    try {
      child.nudge(text);
      return true;
    } catch {
      return false;
    }
  };

  /** Resolve a child's token to its live run and step, or `TOKEN_INVALID`. */
  const requireToken = (rec: Rec, method: string): { live: LiveRun; step: string } => {
    const token = requireString(rec, "token", method);
    for (const live of lives.values()) {
      for (const [step, t] of live.tokens) {
        if (t === token) {
          live.trackers.get(step)?.touch();
          return { live, step };
        }
      }
    }
    throw domainError("TOKEN_INVALID", `${method}: token does not match a running step`);
  };

  const status: Handler<"status"> = async (params, ctx) => {
    const res = await ledger.status(params, ctx);
    const decorate = (s: RunSummary): RunSummary => {
      const live = lives.get(s.run_id);
      if (!live || live.trackers.size === 0) return s;
      return { ...s, children: [...live.trackers.values()].map((t) => t.snapshot()) };
    };
    return Array.isArray(res) ? res.map(decorate) : decorate(res);
  };

  const nudge: Handler<"nudge"> = (params) => {
    const rec = asRecord(params, "nudge");
    const runId = requireString(rec, "run_id", "nudge");
    const stepId = requireString(rec, "step", "nudge");
    const message = requireString(rec, "message", "nudge");
    rt.requireRunDir(runId);
    return { delivered: nudgeChild(runId, stepId, message) };
  };

  const childReport: Handler<"child_report"> = (params) => {
    const rec = asRecord(params, "child_report");
    const { live, step } = requireToken(rec, "child_report");
    const kind = rec.kind;
    if (typeof kind !== "string" || !(REPORT_KINDS as readonly string[]).includes(kind)) {
      throw new RpcError(
        RPC_INVALID_PARAMS,
        `child_report: kind must be one of ${REPORT_KINDS.join(" | ")}`,
      );
    }
    const text = requireString(rec, "text", "child_report");
    live.trackers.get(step)?.report();
    const ev: EventInput = {
      run_id: live.runId,
      type: "step.progress",
      step,
      kind: kind as ReportKind,
      message: clipReportText(text),
    };
    const data = clipReportData(rec.data);
    if (data) ev.data = data;
    const full = appendEvent(live.runDir, ev);
    return { accepted: true, seq: full.seq };
  };

  const childAsk: Handler<"child_ask"> = async (params, ctx) => {
    const rec = asRecord(params, "child_ask");
    const { live, step } = requireToken(rec, "child_ask");
    const question = requireString(rec, "question", "child_ask");
    const options = optionalStringList(rec, "options", "child_ask");
    const allowText = typeof rec.allow_text === "boolean" ? rec.allow_text : undefined;
    const timeoutMs = clampWait(rec, "child_ask");
    const askId = typeof rec.ask_id === "string" ? rec.ask_id : undefined;
    let ask: PendingAsk | undefined;
    if (askId !== undefined) {
      ask = live.asks.get(askId);
      if (!ask || ask.step !== step) {
        throw domainError("GATE_STALE", `child_ask: question ${askId} is gone`, { ask_id: askId });
      }
    } else if (live.controlMode === "synchronous") {
      const state = readState(live.runDir);
      const value = answerFromDecisions(question, options, state.context.decisions);
      const id = newUlid();
      if (value === undefined) {
        emit(live, {
          type: "warn",
          step,
          message: headline(`${step} asked "${question}": no decision in a synchronous run`),
        });
        const out: ChildAskResult = { ask_id: id, status: "needs-human" };
        return out;
      }
      emit(live, {
        type: "step.progress",
        step,
        kind: "decision",
        message: headline(`asked "${question}" -> ${value} (synchronous)`),
      });
      const out: ChildAskResult = { ask_id: id, status: "answered", value };
      return out;
    } else {
      ask = { ask_id: newUlid(), step, question, dropped: false, waiters: new Set() };
      if (options !== undefined) ask.options = options;
      if (allowText !== undefined) ask.allow_text = allowText;
      live.asks.set(ask.ask_id, ask);
      openChildAsks(live);
    }
    await awaitAsk(live, ask, timeoutMs, ctx);
    if (ask.value !== undefined) {
      live.asks.delete(ask.ask_id);
      const out: ChildAskResult = { ask_id: ask.ask_id, status: "answered", value: ask.value };
      return out;
    }
    if (ask.dropped) {
      throw domainError("GATE_STALE", `child_ask: question ${ask.ask_id} was dropped`, {
        ask_id: ask.ask_id,
      });
    }
    const out: ChildAskResult = { ask_id: ask.ask_id, status: "pending" };
    return out;
  };

  const childContext: Handler<"child_context"> = (params) => {
    const rec = asRecord(params, "child_context");
    const { live } = requireToken(rec, "child_context");
    const key = requireString(rec, "key", "child_context");
    return { value: resolveContextKey(readState(live.runDir), key) };
  };

  const childCheckpoint: Handler<"child_checkpoint"> = (params) => {
    const rec = asRecord(params, "child_checkpoint");
    const { live, step } = requireToken(rec, "child_checkpoint");
    if (!("data" in rec)) {
      throw new RpcError(RPC_INVALID_PARAMS, "child_checkpoint: data is required");
    }
    return { path: writeCheckpoint(live.runDir, step, rec.data) };
  };

  const handlers: DaemonHandlers = {
    preflight,
    run,
    wait: ledger.wait,
    answer,
    status,
    cancel,
    resume,
    report,
    nudge,
    child_report: childReport,
    child_ask: childAsk,
    child_context: childContext,
    child_checkpoint: childCheckpoint,
  };

  // ---- lifecycle --------------------------------------------------------------------------------------

  const pickUp = (): string[] => {
    const picked: string[] = [];
    for (const dir of rt.listRunDirs()) {
      let state: State;
      try {
        state = readState(dir);
      } catch {
        continue;
      }
      if (state.status !== "running" || lives.has(state.run_id)) continue;
      try {
        const live = ensureLive(dir, state);
        picked.push(state.run_id);
        setImmediate(() => schedule(live));
      } catch (err) {
        rt.log(`pickup: run ${state.run_id} skipped: ${(err as Error).message}`);
      }
    }
    return picked;
  };

  const stop = (): void => {
    for (const live of lives.values()) {
      for (const child of live.children.values()) child.kill?.("SIGTERM");
      dropLive(live);
    }
    for (const park of parked.values()) clearTimeout(park.timer);
    parked.clear();
  };

  return {
    handlers,
    isBusy: () =>
      inFlightGlobal > 0 || parked.size > 0 || [...lives.values()].some((l) => l.children.size > 0),
    pickUp,
    stop,
    stepToken: (runId, stepId) => lives.get(runId)?.tokens.get(stepId),
    stepByToken: (token) => {
      for (const live of lives.values()) {
        for (const [step, t] of live.tokens) if (t === token) return { run_id: live.runId, step };
      }
      return undefined;
    },
    nudge: nudgeChild,
    liveRuns: () => [...lives.keys()],
  };
}

/** Handler factory for `startDaemon({ handlers })`; `onCreate` hands the executor back to the caller. */
export function executorHandlers(
  opts: ExecutorOptions = {},
  onCreate?: (executor: Executor) => void,
): (rt: DaemonRuntime) => DaemonHandlers {
  return (rt) => {
    const executor = createExecutor(rt, opts);
    onCreate?.(executor);
    return executor.handlers;
  };
}
