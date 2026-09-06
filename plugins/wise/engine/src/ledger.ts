// Ledger: run directory layout, state.json, events.jsonl, unit checkpoints, logs.
// Port of the state/run-history half of plugins/wise/scripts/workflows.py onto the
// v2 State shape (docs/wise/research-ts-engine.md P3). Every function takes explicit
// roots or an options object so tests never touch the real ~/.local/share.

import { randomBytes } from "node:crypto";
import { realpathLoose } from "./paths.ts";
import type { Env } from "./paths.ts";
import { execFileSync } from "node:child_process";
import {
  appendFileSync,
  closeSync,
  cpSync,
  existsSync,
  fstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  readSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";

import { basename, dirname, isAbsolute, join, relative, sep } from "node:path";
import { EMPTY_USAGE } from "./types.ts";
import type {
  Answers,
  AuthMode,
  Context,
  CostSource,
  Event,
  Harness,
  Permissions,
  ProfileLevel,
  Project,
  Resolved,
  RunStatus,
  RunSummary,
  State,
  StepState,
  UnitLedger,
  Usage,
  UsageByPool,
} from "./types.ts";
import { TERMINAL_RUN } from "./types.ts";

// ---- constants ---------------------------------------------------------------

export const RUN_HISTORY_CAP_DEFAULT = 25;
export const SESSION_STALE_SECS_DEFAULT = 1800;

/** Runs the history cap may reclaim. `failed` is resumable, so it stays protected (v1 rule). */
/** History terminal set lives in types.ts (`TERMINAL_RUN`); kept as an alias for callers. */
export const HISTORY_TERMINAL_RUN: ReadonlySet<RunStatus> = TERMINAL_RUN;

export const STEP_ID_RE = /^[a-z][a-z0-9_-]*$/;
const STEP_RUN_ID_RE = /^[A-Za-z0-9_-]+$/;
const ISO_SECONDS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

export { cwdSlug, pluginDataRoot, runDirFor, wiseDataRoot, wiseRunsRootForCwd } from "./paths.ts";
export type { Env, RootOpts } from "./paths.ts";
export type ClockOpts = { now?: string };

export type LedgerErrorCode =
  | "INVALID_STEP_ID"
  | "DUPLICATE_STEP_ID"
  | "NO_SUCH_STEP"
  | "INVALID_STEP_RUN_ID";

export class LedgerError extends Error {
  readonly code: LedgerErrorCode;
  constructor(code: LedgerErrorCode, message: string) {
    super(message);
    this.name = "LedgerError";
    this.code = code;
  }
}

// ---- utilities ---------------------------------------------------------------

/** `YYYY-MM-DDTHH:MM:SSZ`, one-second granularity like the Python `utc_now`. */
export function utcNow(date: Date = new Date()): string {
  return date.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Positive-int env override; unset, non-numeric, or non-positive falls back to `dflt`. */
export function envPositiveInt(name: string, dflt: number, env: Env = process.env): number {
  const raw = env[name];
  if (raw === undefined) return dflt;
  const trimmed = raw.trim();
  if (!/^[+-]?\d+$/.test(trimmed)) return dflt;
  const value = Number(trimmed);
  return value > 0 ? value : dflt;
}

/** Whether an ISO activity stamp is within `staleAfter` seconds of now. Missing or malformed is stale. */
export function sessionIsFresh(
  isoTs: string | null | undefined,
  staleAfter: number,
  nowMs: number = Date.now(),
): boolean {
  if (!isoTs || !ISO_SECONDS_RE.test(isoTs)) return false;
  const ms = Date.parse(isoTs);
  if (Number.isNaN(ms)) return false;
  return (nowMs - ms) / 1000 <= staleAfter;
}

// ---- ULID ----------------------------------------------------------------------

const B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const RAND_MOD = 1n << 80n;
let ulidLastMs = -1;
let ulidLastRand = 0n;

/** Crockford-base32 ULID, monotonic within this process (same-ms ULIDs increment the random part). */
export function newUlid(nowMs: number = Date.now()): string {
  let t = Math.max(0, Math.floor(nowMs));
  if (t <= ulidLastMs) {
    t = ulidLastMs;
    ulidLastRand = (ulidLastRand + 1n) % RAND_MOD;
  } else {
    ulidLastMs = t;
    ulidLastRand = BigInt("0x" + randomBytes(10).toString("hex"));
  }
  let time = "";
  let tv = t;
  for (let i = 0; i < 10; i++) {
    time = (B32[tv % 32] ?? "0") + time;
    tv = Math.floor(tv / 32);
  }
  let rand = "";
  let rv = ulidLastRand;
  for (let i = 0; i < 16; i++) {
    rand = (B32[Number(rv % 32n)] ?? "0") + rand;
    rv /= 32n;
  }
  return time + rand;
}

// ---- state.json --------------------------------------------------------------------

export function statePath(runDir: string): string {
  return join(runDir, "state.json");
}

function writeJsonAtomic(path: string, data: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = path + ".tmp";
  try {
    writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", "utf8");
    renameSync(tmp, path);
  } catch (err) {
    rmSync(tmp, { force: true });
    throw err;
  }
}

/** Write `state.json` atomically: `state.json.tmp` then rename. */
export function writeState(runDir: string, state: State): void {
  writeJsonAtomic(statePath(runDir), state);
}

export function readState(runDir: string): State {
  return JSON.parse(readFileSync(statePath(runDir), "utf8")) as State;
}

type LooseState = Partial<State> & Record<string, unknown>;

function readStateLoose(runDir: string): LooseState | null {
  try {
    const parsed: unknown = JSON.parse(readFileSync(statePath(runDir), "utf8"));
    return parsed && typeof parsed === "object" ? (parsed as LooseState) : {};
  } catch {
    return null;
  }
}

function workflowName(s: LooseState): string | undefined {
  const wf = s.workflow;
  return wf && typeof wf === "object" && typeof wf.name === "string" ? wf.name : undefined;
}

export function emptyUsageByPool(): UsageByPool {
  return {
    subscription: EMPTY_USAGE("subscription"),
    "api-key": EMPTY_USAGE("api-key"),
    by_harness: {},
    by_step: {},
  };
}

// ---- usage folding (E14, M6.1) ------------------------------------------------------------

/** Aggregate label: `none` until a cost lands, `reported` while every part was, `priced` after. */
function foldCostSource(into: Usage, u: Usage): void {
  const incoming: CostSource = u.cost_source ?? (u.cost_usd !== undefined ? "reported" : "none");
  if (incoming === "none") {
    into.cost_source ??= "none";
    return;
  }
  const current = into.cost_source ?? "none";
  into.cost_source = current === "none" ? incoming : current === incoming ? current : "priced";
}

/** The one place a `Usage` is added to another; every view below goes through it. */
export function addUsage(into: Usage, u: Usage): void {
  into.input += u.input;
  into.output += u.output;
  into.cache_read += u.cache_read;
  into.cache_write += u.cache_write;
  if (u.cost_usd !== undefined) into.cost_usd = (into.cost_usd ?? 0) + u.cost_usd;
  foldCostSource(into, u);
}

/**
 * Fold one child's usage into every run view in one go: its pool, its harness, its step and the
 * step's own `usage`. Old state files without `by_step` grow the view on first use. Pure on the
 * given state; the caller writes it.
 */
export function foldUsageViews(
  state: State,
  fold: { step: string; harness: Harness; usage: Usage },
): void {
  const { step, harness, usage } = fold;
  const pool: AuthMode = usage.pool;
  addUsage(state.usage[pool], usage);
  addUsage((state.usage.by_harness[harness] ??= EMPTY_USAGE(pool)), usage);
  state.usage.by_step ??= {};
  addUsage((state.usage.by_step[step] ??= EMPTY_USAGE(pool)), usage);
  const st = state.steps[step];
  if (st) addUsage((st.usage ??= EMPTY_USAGE(pool)), usage);
}

/** Both pools folded into one figure; `pool` is `subscription` unless only api-key usage exists. */
export function usageTotal(u: UsageByPool): Usage {
  const sub = u.subscription;
  const api = u["api-key"];
  const total = EMPTY_USAGE(
    usageTokens(sub) === 0 && usageTokens(api) > 0 ? "api-key" : "subscription",
  );
  addUsage(total, sub);
  addUsage(total, api);
  return total;
}

/**
 * Tokens that count against a run ceiling (E11): input, output and cache writes. Cache reads are
 * excluded on purpose: a Claude child re-reads its whole context (the 27k stock prompt included)
 * on every turn at a tenth of the price, so they measure context size, not new work, and would
 * trip any ceiling within a few turns.
 */
export function usageTokens(u: Usage): number {
  return u.input + u.output + u.cache_write;
}

export type InitStateArgs = ClockOpts & {
  runDir: string;
  runId: string;
  workflow: { name: string; version: number; dir: string };
  stepIds: readonly string[];
  cwd?: string;
  harnessSession?: string | null;
  profile?: ProfileLevel;
};

/** Phase-A init: stub state with `status: initializing`, every step `pending`, plus `logs/`. */
export function initState(args: InitStateArgs): State {
  const seen = new Set<string>();
  const steps: Record<string, StepState> = {};
  for (const id of args.stepIds) {
    if (!STEP_ID_RE.test(id)) {
      throw new LedgerError(
        "INVALID_STEP_ID",
        `step-id ${JSON.stringify(id)} must match ${STEP_ID_RE}`,
      );
    }
    if (seen.has(id)) {
      throw new LedgerError("DUPLICATE_STEP_ID", `duplicate step-id ${JSON.stringify(id)}`);
    }
    seen.add(id);
    steps[id] = { status: "pending", attempts: 0 };
  }
  const now = args.now ?? utcNow();
  const state: State = {
    version: 2,
    run_id: args.runId,
    workflow: args.workflow,
    cwd: args.cwd ?? process.cwd(),
    project: null,
    ...(args.harnessSession ? { harness_session: args.harnessSession } : {}),
    status: "initializing",
    profile: args.profile ?? "medium",
    answers: {},
    context: {},
    inputs: {},
    resolved: {},
    caps: {},
    usage: emptyUsageByPool(),
    steps,
    outputs: {},
    started_at: now,
    last_activity_at: now,
  };
  mkdirSync(join(args.runDir, "logs"), { recursive: true });
  writeState(args.runDir, state);
  return state;
}

export type StartRunCtx = {
  project?: Project | null;
  /** Merged into `outputs` too, so `{{name}}` templates resolve like captured outputs (v1 rule). */
  inputs?: Record<string, string>;
  answers?: Answers;
  context?: Context;
  profile?: ProfileLevel;
  permissions?: Permissions;
  resolved?: Record<string, Resolved>;
  caps?: Record<string, number>;
  harnessSession?: string;
};

/** Phase-B init: record pre-flight answers and flip to `running`. */
export function startRun(runDir: string, ctx: StartRunCtx, opts: ClockOpts = {}): State {
  const state = readState(runDir);
  if (ctx.project !== undefined) state.project = ctx.project;
  if (ctx.answers) state.answers = { ...state.answers, ...ctx.answers };
  if (ctx.context) state.context = { ...state.context, ...ctx.context };
  if (ctx.profile) state.profile = ctx.profile;
  if (ctx.permissions) state.permissions = ctx.permissions;
  if (ctx.resolved) state.resolved = { ...state.resolved, ...ctx.resolved };
  if (ctx.caps) state.caps = { ...state.caps, ...ctx.caps };
  if (ctx.harnessSession) state.harness_session = ctx.harnessSession;
  if (ctx.inputs && Object.keys(ctx.inputs).length > 0) {
    state.inputs = { ...state.inputs, ...ctx.inputs };
    state.outputs = { ...state.outputs, ...ctx.inputs };
  }
  state.status = "running";
  state.last_activity_at = opts.now ?? utcNow();
  writeState(runDir, state);
  return state;
}

function stepOrThrow(state: State, stepId: string): StepState {
  const step = state.steps[stepId];
  if (!step) throw new LedgerError("NO_SUCH_STEP", `no such step: ${stepId}`);
  return step;
}

export function updateStep(
  runDir: string,
  stepId: string,
  patch: Partial<StepState>,
  opts: ClockOpts = {},
): State {
  const state = readState(runDir);
  Object.assign(stepOrThrow(state, stepId), patch);
  state.last_activity_at = opts.now ?? utcNow();
  writeState(runDir, state);
  return state;
}

/** Mark a step `running` with a fresh step ULID (a re-run never reuses the old one). */
export function startStep(runDir: string, stepId: string, opts: ClockOpts = {}): string {
  const state = readState(runDir);
  const step = stepOrThrow(state, stepId);
  const stepRunId = newUlid();
  const now = opts.now ?? utcNow();
  step.status = "running";
  step.step_run_id = stepRunId;
  step.started_at = now;
  step.attempts = (step.attempts ?? 0) + 1;
  delete step.completed_at;
  delete step.verdict;
  delete step.error;
  state.last_activity_at = now;
  writeState(runDir, state);
  return stepRunId;
}

export function updateRun(runDir: string, patch: Partial<State>, opts: ClockOpts = {}): State {
  const state = readState(runDir);
  Object.assign(state, patch);
  state.last_activity_at = opts.now ?? utcNow();
  writeState(runDir, state);
  return state;
}

export function recordOutput(
  runDir: string,
  name: string,
  value: unknown,
  opts: ClockOpts = {},
): State {
  const state = readState(runDir);
  state.outputs = { ...state.outputs, [name]: value };
  state.last_activity_at = opts.now ?? utcNow();
  writeState(runDir, state);
  return state;
}

/** Resume: every `running` step goes back to `pending` (its ULID and start stamp dropped); run re-armed. */
export function resetRunning(runDir: string, opts: ClockOpts = {}): State {
  const state = readState(runDir);
  for (const step of Object.values(state.steps ?? {})) {
    if (step.status === "running") {
      step.status = "pending";
      delete step.started_at;
      delete step.step_run_id;
    }
  }
  state.status = "running";
  state.last_activity_at = opts.now ?? utcNow();
  writeState(runDir, state);
  return state;
}

export function dumpState(runDir: string): string {
  return JSON.stringify(readState(runDir), null, 2);
}

// ---- run listing / history ------------------------------------------------------------

export type RunRow = {
  run_id: string;
  status: string;
  workflow: string;
  last_activity_at: string;
};

function runDirs(runsRoot: string): string[] {
  let names: string[];
  try {
    names = readdirSync(runsRoot);
  } catch {
    return [];
  }
  return names.toSorted().map((n) => join(runsRoot, n));
}

/** Every run dir under `runsRoot` that holds a state file, in name order. Unreadable states are flagged. */
export function listRuns(runsRoot: string): RunRow[] {
  const rows: RunRow[] = [];
  for (const dir of runDirs(runsRoot)) {
    if (!existsSync(statePath(dir))) continue;
    const s = readStateLoose(dir);
    if (s === null) {
      rows.push({
        run_id: basename(dir),
        status: "<unreadable>",
        workflow: "",
        last_activity_at: "",
      });
      continue;
    }
    rows.push({
      run_id: basename(dir),
      status: typeof s.status === "string" ? s.status : "?",
      workflow: workflowName(s) ?? "?",
      last_activity_at: typeof s.last_activity_at === "string" ? s.last_activity_at : "?",
    });
  }
  return rows;
}

/** The v1 `list-runs` table text. */
export function formatRunsTable(rows: RunRow[]): string {
  if (rows.length === 0) return "(no runs in this workspace yet)";
  const lines = [
    `${"RUN ID".padEnd(26)}  ${"STATUS".padEnd(10)}  ${"WORKFLOW".padEnd(24)}  LAST ACTIVITY`,
  ];
  for (const r of rows) {
    lines.push(
      `${r.run_id.padEnd(26)}  ${r.status.padEnd(10)}  ${r.workflow.padEnd(24)}  ${r.last_activity_at}`,
    );
  }
  return lines.join("\n");
}

function summaryOf(dir: string, s: LooseState): RunSummary {
  const summary: RunSummary = {
    run_id: typeof s.run_id === "string" && s.run_id ? s.run_id : basename(dir),
    workflow: workflowName(s) ?? "",
    status: (typeof s.status === "string" ? s.status : "initializing") as RunStatus,
    started_at: typeof s.started_at === "string" ? s.started_at : "",
    last_activity_at: typeof s.last_activity_at === "string" ? s.last_activity_at : "",
    cwd: typeof s.cwd === "string" ? s.cwd : "",
  };
  if (typeof s.completed_at === "string") summary.completed_at = s.completed_at;
  if (s.gate) summary.gate = s.gate;
  return summary;
}

/** Non-terminal runs (initializing/running/gated/paused/failed), most recently active first. */
export function listResumableRuns(runsRoot: string): RunSummary[] {
  const items: RunSummary[] = [];
  for (const dir of runDirs(runsRoot)) {
    if (!existsSync(statePath(dir))) continue;
    const s = readStateLoose(dir);
    if (s === null) continue;
    if (HISTORY_TERMINAL_RUN.has(s.status as RunStatus)) continue;
    items.push(summaryOf(dir, s));
  }
  items.sort((a, b) =>
    a.last_activity_at < b.last_activity_at ? 1 : a.last_activity_at > b.last_activity_at ? -1 : 0,
  );
  return items;
}

export type PruneResult = { pruned: string[]; failed: { run_id: string; reason: string }[] };

type PruneEntry = { last: string; terminal: boolean; path: string; name: string };

function newestFirst(a: PruneEntry, b: PruneEntry): number {
  if (a.last !== b.last) return a.last < b.last ? 1 : -1;
  if (a.name !== b.name) return a.name < b.name ? 1 : -1;
  return 0;
}

/**
 * Cap the per-workspace history at WISE_RUN_HISTORY_CAP (default 25). Non-terminal runs are always
 * kept; the remaining budget goes to the most recently active terminal runs, the rest are deleted.
 * Orphan dirs (no readable state) count as the oldest terminal runs.
 */
export function pruneRuns(runsRoot: string, env: Env = process.env): PruneResult {
  const result: PruneResult = { pruned: [], failed: [] };
  const cap = envPositiveInt("WISE_RUN_HISTORY_CAP", RUN_HISTORY_CAP_DEFAULT, env);
  let root: string;
  try {
    root = realpathSync(runsRoot);
    if (!statSync(root).isDirectory()) return result;
  } catch {
    return result;
  }

  const entries: PruneEntry[] = [];
  for (const child of runDirs(root)) {
    let isDir = false;
    try {
      isDir = statSync(child).isDirectory();
    } catch {
      continue;
    }
    if (!isDir) continue;
    const name = basename(child);
    if (!existsSync(statePath(child))) {
      entries.push({ last: "", terminal: true, path: child, name });
      continue;
    }
    const s = readStateLoose(child);
    if (s === null) {
      entries.push({ last: "", terminal: true, path: child, name });
      continue;
    }
    const last =
      (typeof s.last_activity_at === "string" && s.last_activity_at) ||
      (typeof s.started_at === "string" && s.started_at) ||
      "";
    entries.push({
      last,
      terminal: HISTORY_TERMINAL_RUN.has(s.status as RunStatus),
      path: child,
      name,
    });
  }

  if (entries.length <= cap) return result;

  const nonTerm = entries.filter((e) => !e.terminal);
  const term = entries.filter((e) => e.terminal).toSorted(newestFirst);
  const toDelete = term.slice(Math.max(0, cap - nonTerm.length));

  for (const e of toDelete) {
    let resolved: string;
    try {
      resolved = realpathSync(e.path);
    } catch {
      continue;
    }
    // Safety rail: never remove anything outside the runs root.
    if (!resolved.startsWith(root + sep)) continue;
    try {
      rmSync(e.path, { recursive: true, force: true });
      result.pruned.push(e.name);
    } catch (err) {
      result.failed.push({ run_id: e.name, reason: String(err) });
    }
  }
  return result;
}

export type SessionRunRow = {
  run_id: string;
  workflow: string;
  status: string;
  last_activity_at: string | null;
  /** `true` = checked in within WISE_SESSION_STALE_SECS, a genuine conflict; `false` = abandoned. */
  fresh: boolean;
};

/** Non-terminal runs under `runsRoot` whose `harness_session` is `sessionId`, in dir-name order. */
export function findRunsBySession(
  runsRoot: string,
  sessionId: string,
  env: Env = process.env,
  nowMs: number = Date.now(),
): SessionRunRow[] {
  const staleAfter = envPositiveInt("WISE_SESSION_STALE_SECS", SESSION_STALE_SECS_DEFAULT, env);
  const rows: SessionRunRow[] = [];
  for (const dir of runDirs(runsRoot)) {
    if (!existsSync(statePath(dir))) continue;
    const s = readStateLoose(dir);
    if (s === null) continue;
    if (s.harness_session !== sessionId) continue;
    if (HISTORY_TERMINAL_RUN.has(s.status as RunStatus)) continue;
    const last = typeof s.last_activity_at === "string" ? s.last_activity_at : null;
    rows.push({
      run_id: typeof s.run_id === "string" && s.run_id ? s.run_id : basename(dir),
      workflow: workflowName(s) ?? "?",
      status: typeof s.status === "string" ? s.status : "?",
      last_activity_at: last,
      fresh: sessionIsFresh(last, staleAfter, nowMs),
    });
  }
  return rows;
}

/** The v1 `find-runs-by-session` line: tab-separated, `fresh|stale` last. */
export function formatSessionRunRow(r: SessionRunRow): string {
  return [
    r.run_id,
    r.workflow,
    r.status,
    r.last_activity_at ?? "?",
    r.fresh ? "fresh" : "stale",
  ].join("\t");
}

// ---- events.jsonl -------------------------------------------------------------------

export function eventsPath(runDir: string): string {
  return join(runDir, "events.jsonl");
}

const TAIL_BYTES = 65536;

/** Read the tail of a file; falls back to the whole file when the tail holds no complete line. */
function readTail(path: string): string {
  const fd = openSync(path, "r");
  try {
    const size = fstatSync(fd).size;
    if (size === 0) return "";
    const start = Math.max(0, size - TAIL_BYTES);
    const buf = Buffer.alloc(size - start);
    readSync(fd, buf, 0, buf.length, start);
    const text = buf.toString("utf8");
    if (start === 0 || text.includes("\n")) return text;
  } finally {
    closeSync(fd);
  }
  return readFileSync(path, "utf8");
}

function endsWithNewline(path: string): boolean {
  let fd: number;
  try {
    fd = openSync(path, "r");
  } catch {
    return true;
  }
  try {
    const size = fstatSync(fd).size;
    if (size === 0) return true;
    const buf = Buffer.alloc(1);
    readSync(fd, buf, 0, 1, size - 1);
    return buf[0] === 0x0a;
  } finally {
    closeSync(fd);
  }
}

function lastSeq(runDir: string): number {
  const path = eventsPath(runDir);
  if (!existsSync(path)) return 0;
  const lines = readTail(path)
    .split("\n")
    .filter((l) => l.trim() !== "");
  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      const ev = JSON.parse(lines[i] ?? "") as { seq?: unknown };
      if (typeof ev.seq === "number") return ev.seq;
    } catch {
      // A torn trailing line; keep walking back.
    }
  }
  return 0;
}

export type EventInput = Omit<Event, "seq" | "ts">;

/** Append one event with the next `seq` (derived from the file, so it survives restarts). */
export function appendEvent(runDir: string, event: EventInput, opts: ClockOpts = {}): Event {
  mkdirSync(runDir, { recursive: true });
  const path = eventsPath(runDir);
  const full: Event = { ...event, seq: lastSeq(runDir) + 1, ts: opts.now ?? utcNow() };
  // A torn last line (crash mid-write) must not swallow the new record.
  const lead = endsWithNewline(path) ? "" : "\n";
  appendFileSync(path, lead + JSON.stringify(full) + "\n", "utf8");
  return full;
}

/** Events with `seq > after`, in file order. */
export function readEvents(runDir: string, after = 0): Event[] {
  const path = eventsPath(runDir);
  if (!existsSync(path)) return [];
  const out: Event[] = [];
  for (const line of readFileSync(path, "utf8").split("\n")) {
    if (line.trim() === "") continue;
    let ev: Event;
    try {
      ev = JSON.parse(line) as Event;
    } catch {
      continue;
    }
    if (ev.seq > after) out.push(ev);
  }
  return out;
}

// ---- units/<branch>.json -------------------------------------------------------------

/** Branch names may contain `/`; encode so one unit is always one flat file. */
export function unitPath(runDir: string, branch: string): string {
  return join(runDir, "units", encodeURIComponent(branch) + ".json");
}

export function readUnit(runDir: string, branch: string): UnitLedger | null {
  const path = unitPath(runDir, branch);
  if (!existsSync(path)) return null;
  return JSON.parse(readFileSync(path, "utf8")) as UnitLedger;
}

export function writeUnit(runDir: string, branch: string, ledger: UnitLedger): void {
  writeJsonAtomic(unitPath(runDir, branch), ledger);
}

export function listUnits(runDir: string): UnitLedger[] {
  const dir = join(runDir, "units");
  let names: string[];
  try {
    names = readdirSync(dir);
  } catch {
    return [];
  }
  const out: UnitLedger[] = [];
  for (const name of names.toSorted()) {
    if (!name.endsWith(".json") || name.endsWith(".json.tmp")) continue;
    try {
      out.push(JSON.parse(readFileSync(join(dir, name), "utf8")) as UnitLedger);
    } catch {
      continue;
    }
  }
  return out;
}

// ---- logs -----------------------------------------------------------------------------

function checkLogIds(stepId: string, stepRunId: string): void {
  if (!STEP_ID_RE.test(stepId)) {
    throw new LedgerError(
      "INVALID_STEP_ID",
      `step-id ${JSON.stringify(stepId)} must match ${STEP_ID_RE}`,
    );
  }
  if (!STEP_RUN_ID_RE.test(stepRunId)) {
    throw new LedgerError(
      "INVALID_STEP_RUN_ID",
      `step-run-id ${JSON.stringify(stepRunId)} must be a bare alphanumeric/_/- token`,
    );
  }
}

/** `logs/<step>.<ulid>.raw.jsonl` (vendor stream) and `logs/<step>.<ulid>.log` (human extract). */
export function logPaths(
  runDir: string,
  stepId: string,
  stepRunId: string,
): { raw: string; log: string } {
  checkLogIds(stepId, stepRunId);
  const base = join(runDir, "logs", `${stepId}.${stepRunId}`);
  return { raw: `${base}.raw.jsonl`, log: `${base}.log` };
}

/** Write the human-readable step log; returns its path. */
export function writeLog(
  runDir: string,
  stepId: string,
  stepRunId: string,
  content: string,
): string {
  const { log } = logPaths(runDir, stepId, stepRunId);
  mkdirSync(dirname(log), { recursive: true });
  writeFileSync(log, content, "utf8");
  return log;
}

/** Append one record to the raw vendor stream as a JSON line; returns the file path. */
export function appendRawLog(
  runDir: string,
  stepId: string,
  stepRunId: string,
  record: unknown,
): string {
  const { raw } = logPaths(runDir, stepId, stepRunId);
  mkdirSync(dirname(raw), { recursive: true });
  appendFileSync(raw, JSON.stringify(record) + "\n", "utf8");
  return raw;
}

// ---- apply-worktree-include -------------------------------------------------------------

export type Exec = (cmd: string, args: string[]) => string;

function inside(base: string, p: string): boolean {
  const rel = relative(base, p);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

const defaultExec: Exec = (cmd, args) =>
  execFileSync(cmd, args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });

export type WorktreeIncludeResult = { copied: number; skipped: number; notices: string[] };

/**
 * Copy `.worktreeinclude`-listed untracked/ignored files from `repoRoot` into `worktreeDir`.
 * Matching is delegated to `git ls-files --others --ignored --exclude-from=.worktreeinclude`, so
 * tracked files are never touched. Existing destination files are overwritten. Best-effort: never
 * throws, every problem lands in `notices`.
 */
export function applyWorktreeInclude(
  repoRoot: string,
  worktreeDir: string,
  opts: { exec?: Exec } = {},
): WorktreeIncludeResult {
  const exec = opts.exec ?? defaultExec;
  const result: WorktreeIncludeResult = { copied: 0, skipped: 0, notices: [] };
  const inc = join(repoRoot, ".worktreeinclude");
  let incIsFile = false;
  try {
    incIsFile = statSync(inc).isFile();
  } catch {
    incIsFile = false;
  }
  if (!incIsFile) {
    result.notices.push("worktree-include: no .worktreeinclude - nothing to copy");
    return result;
  }

  let stdout: string;
  try {
    stdout = exec("git", [
      "-C",
      repoRoot,
      "ls-files",
      "-z",
      "--others",
      "--ignored",
      "--directory",
      "--no-empty-directory",
      `--exclude-from=${inc}`,
    ]);
  } catch (err) {
    result.notices.push(`worktree-include: git ls-files failed (${String(err)}); skipping`);
    return result;
  }

  const entries = stdout.split("\0").filter((e) => e !== "");
  let rootRes: string;
  let destRes: string;
  try {
    rootRes = realpathSync(repoRoot);
    destRes = realpathSync(worktreeDir);
  } catch (err) {
    result.notices.push(`worktree-include: cannot resolve paths (${String(err)}); skipping`);
    return result;
  }

  for (const rel of entries) {
    const src = join(repoRoot, rel);
    const dst = join(worktreeDir, rel);
    // Stay inside repoRoot / worktreeDir; a symlink or `..` that escapes is refused.
    if (!inside(rootRes, realpathLoose(src)) || !inside(destRes, realpathLoose(dst))) {
      result.notices.push(`worktree-include: skip out-of-tree path ${JSON.stringify(rel)}`);
      result.skipped++;
      continue;
    }
    let isDir: boolean;
    try {
      isDir = statSync(src).isDirectory();
    } catch {
      result.skipped++;
      continue;
    }
    try {
      if (rel.endsWith("/") || isDir) {
        cpSync(src, dst, {
          recursive: true,
          force: true,
          verbatimSymlinks: true,
          preserveTimestamps: true,
        });
      } else {
        mkdirSync(dirname(dst), { recursive: true });
        cpSync(src, dst, { force: true, dereference: true, preserveTimestamps: true });
      }
      result.copied++;
    } catch (err) {
      result.notices.push(
        `worktree-include: failed to copy ${JSON.stringify(rel)} (${String(err)})`,
      );
      result.skipped++;
    }
  }

  result.notices.push(`worktree-include: copied ${result.copied} (skipped ${result.skipped})`);
  return result;
}
