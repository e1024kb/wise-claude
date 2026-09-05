// Child channel helpers (research-ts-engine.md P8, D16): live status derived from a child's event
// stream, the throttle behind `step.progress`, the stale watchdog, report clipping, the context
// lookup behind `wise_context`, checkpoints, and the synchronous-mode answer for `wise_ask`.
// Everything here is pure or injectable; the executor wires it to real children and the ledger.

import { mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { headline } from "./steps/agent.ts";
import type { ChildProgress, RawEvent, State } from "./types.ts";

export const PROGRESS_THROTTLE_MS = 30_000;
export const STALE_AFTER_SECS_DEFAULT = 600;
export const REPORT_TEXT_MAX = 200;
export const REPORT_DATA_MAX_BYTES = 1024;

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

// ---- timers ---------------------------------------------------------------------------------------

/** Clock and timers, injectable so tests drive the stale policy without waiting. */
export type ChannelTimers = {
  now: () => number;
  setTimeout: (fn: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
};

export const realTimers: ChannelTimers = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => {
    const t = setTimeout(fn, ms);
    t.unref?.();
    return t;
  },
  clearTimeout: (h) => clearTimeout(h as NodeJS.Timeout),
};

// ---- live status from the stream --------------------------------------------------------------------

/** Sum of the `*_tokens` counters in a vendor usage object (Claude and Codex shapes). */
function usageTokens(u: unknown): number {
  if (!isRec(u)) return 0;
  let total = 0;
  for (const [k, v] of Object.entries(u)) if (k.endsWith("_tokens")) total += num(v);
  return total;
}

function toolUses(message: unknown): string[] {
  if (!isRec(message) || !Array.isArray(message.content)) return [];
  return message.content
    .filter((b): b is Rec => isRec(b) && b.type === "tool_use")
    .map((b) => (typeof b.name === "string" ? b.name : "?"));
}

export type ChildTracker = {
  /** Feed one vendor event; returns a snapshot when a `step.progress` event is due. */
  ingest: (e: RawEvent) => ChildProgress | undefined;
  /** A `wise_report` arrived. */
  report: () => void;
  /** Any other channel call: counts as activity. */
  touch: () => void;
  snapshot: () => ChildProgress;
  lastActivityMs: () => number;
};

export type TrackerOpts = { step: string; throttleMs?: number; now?: () => number };

/**
 * Derives turn count, last tool, tokens so far and last activity from the raw stream. Emits at most
 * one progress snapshot per `throttleMs` per step, or at once when the tool name changes.
 */
export function createChildTracker(opts: TrackerOpts): ChildTracker {
  const now = opts.now ?? (() => Date.now());
  const throttleMs = opts.throttleMs ?? PROGRESS_THROTTLE_MS;
  let turn = 0;
  let tool: string | undefined;
  let tokens = 0;
  let reports = 0;
  let lastActivity = now();
  let lastEmit = lastActivity;

  const snapshot = (): ChildProgress => {
    const snap: ChildProgress = {
      step: opts.step,
      turn,
      tokens,
      last_activity: new Date(lastActivity).toISOString(),
      reports,
    };
    if (tool !== undefined) snap.tool = tool;
    return snap;
  };

  return {
    ingest(e) {
      lastActivity = now();
      const parsed = e.parsed;
      if (!isRec(parsed)) return undefined;
      const before = tool;
      if (parsed.type === "assistant") {
        turn += 1;
        const uses = toolUses(parsed.message);
        if (uses.length > 0) tool = uses[uses.length - 1];
        if (isRec(parsed.message)) tokens += usageTokens(parsed.message.usage);
      } else if (parsed.type === "result") {
        // Cumulative totals from the harness win over the per-turn sum.
        tokens = Math.max(tokens, usageTokens(parsed.usage));
      } else if (parsed.usage !== undefined) {
        tokens += usageTokens(parsed.usage);
      }
      const changed = tool !== before;
      if (!changed && lastActivity - lastEmit < throttleMs) return undefined;
      lastEmit = lastActivity;
      return snapshot();
    },
    report() {
      reports += 1;
      lastActivity = now();
    },
    touch() {
      lastActivity = now();
    },
    snapshot,
    lastActivityMs: () => lastActivity,
  };
}

function fmtTokens(n: number): string {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

/** One-line rendering for the `step.progress` event: `turn 14, tool Edit, 48k tokens`. */
export function progressLine(p: ChildProgress): string {
  const parts = [`turn ${p.turn}`];
  if (p.tool !== undefined) parts.push(`tool ${p.tool}`);
  parts.push(`${fmtTokens(p.tokens)} tokens`);
  if (p.reports > 0) parts.push(`${p.reports} report${p.reports === 1 ? "" : "s"}`);
  return parts.join(", ");
}

// ---- stale watchdog ------------------------------------------------------------------------------------

export type StaleWatchOpts = {
  staleMs: number;
  timers: ChannelTimers;
  lastActivityMs: () => number;
  /** Idle because a human has not answered its question yet: never stale. */
  paused?: () => boolean;
  /** Deliver the idle nudge; `false` when the harness has no stdin (kill at once, E8). */
  nudge: (idleMs: number) => boolean;
  kill: () => void;
};

export type StaleWatch = { stop: () => void };

/**
 * After `staleMs` of no activity: one nudge when the harness takes one, then a kill after another
 * idle window. Activity between the two resets the policy, so each idle stretch gets its own nudge.
 */
export function startStaleWatch(opts: StaleWatchOpts): StaleWatch {
  const { timers, staleMs } = opts;
  let handle: unknown;
  let nudged = false;
  let stopped = false;

  const arm = (ms: number): void => {
    if (stopped) return;
    handle = timers.setTimeout(fire, Math.max(1, ms));
  };
  const fire = (): void => {
    if (stopped) return;
    if (opts.paused?.()) {
      arm(staleMs);
      return;
    }
    const idle = timers.now() - opts.lastActivityMs();
    if (idle < staleMs) {
      nudged = false;
      arm(staleMs - idle);
      return;
    }
    if (!nudged && opts.nudge(idle)) {
      nudged = true;
      arm(staleMs);
      return;
    }
    stopped = true;
    opts.kill();
  };
  arm(staleMs);
  return {
    stop() {
      stopped = true;
      if (handle !== undefined) timers.clearTimeout(handle);
    },
  };
}

export function staleNudgeText(idleMs: number): string {
  const minutes = Math.max(1, Math.round(idleMs / 60_000));
  return `You have been idle for ${minutes} minute${minutes === 1 ? "" : "s"}. Finish with your structured result now.`;
}

// ---- reports ---------------------------------------------------------------------------------------------

export function clipReportText(text: string): string {
  return headline(text, REPORT_TEXT_MAX);
}

/** The report payload when it serializes under the cap, otherwise nothing. */
export function clipReportData(data: unknown): Record<string, unknown> | undefined {
  if (!isRec(data)) return undefined;
  let json: string;
  try {
    json = JSON.stringify(data);
  } catch {
    return undefined;
  }
  return Buffer.byteLength(json, "utf8") <= REPORT_DATA_MAX_BYTES ? data : undefined;
}

// ---- context lookup --------------------------------------------------------------------------------------

function pathLookup(root: unknown, path: string[]): unknown {
  let cur: unknown = root;
  for (const seg of path) {
    if (Array.isArray(cur) && /^\d+$/.test(seg)) cur = cur[Number(seg)];
    else if (isRec(cur)) cur = cur[seg];
    else return undefined;
    if (cur === undefined) return undefined;
  }
  return cur;
}

/**
 * `wise_context(key)`: run context first (`ticket`, `guidance`, `decisions`, `links`, dotted paths
 * such as `ticket.0.body`), then run outputs, a step's outputs by step id, then inputs. `null` when
 * nothing matches.
 */
export function resolveContextKey(state: State, key: string): unknown {
  const path = key.split(".").filter((s) => s.length > 0);
  if (path.length === 0) return null;
  const fromContext = pathLookup(state.context, path);
  if (fromContext !== undefined) return fromContext;
  const fromOutputs = pathLookup(state.outputs, path);
  if (fromOutputs !== undefined) return fromOutputs;
  const [stepId, ...rest] = path;
  const step = stepId !== undefined ? state.steps[stepId] : undefined;
  if (step?.outputs !== undefined) {
    const fromStep = rest.length === 0 ? step.outputs : pathLookup(step.outputs, rest);
    if (fromStep !== undefined) return fromStep;
  }
  const fromInputs = pathLookup(state.inputs, path);
  return fromInputs === undefined ? null : fromInputs;
}

// ---- checkpoints ------------------------------------------------------------------------------------------

export function checkpointPath(runDir: string, stepId: string): string {
  return join(runDir, "checkpoints", `${stepId}.json`);
}

/** Atomic write of the child's partial result; survives a stale kill. Returns the file path. */
export function writeCheckpoint(runDir: string, stepId: string, data: unknown): string {
  const path = checkpointPath(runDir, stepId);
  mkdirSync(join(runDir, "checkpoints"), { recursive: true });
  const tmp = `${path}.tmp`;
  try {
    writeFileSync(tmp, JSON.stringify(data ?? null, null, 2) + "\n", "utf8");
    renameSync(tmp, path);
  } catch (err) {
    rmSync(tmp, { force: true });
    throw err;
  }
  return path;
}

// ---- synchronous answers -----------------------------------------------------------------------------------

const norm = (s: string): string => s.trim().toLowerCase();

/**
 * Best-effort answer for `wise_ask` in a synchronous run: a decision keyed by the question text,
 * then a decision whose key appears in the question (or the reverse), then a decision whose value
 * is one of the options, then the first option. `undefined` means a human is needed.
 */
export function answerFromDecisions(
  question: string,
  options: readonly string[] | undefined,
  decisions: Record<string, string> | undefined,
): string | undefined {
  const q = norm(question);
  const entries = Object.entries(decisions ?? {});
  const exact = entries.find(([k]) => norm(k) === q);
  if (exact) return exact[1];
  const partial = entries.find(([k]) => {
    const nk = norm(k);
    return nk.length > 0 && (q.includes(nk) || nk.includes(q));
  });
  if (partial) return partial[1];
  if (options && options.length > 0) {
    const byOption = entries.find(([, v]) => options.some((o) => norm(o) === norm(v)));
    if (byOption) return byOption[1];
    return options[0];
  }
  return undefined;
}
