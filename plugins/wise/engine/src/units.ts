// `units` step (plan M4.1): the per-unit loop for the ticket and plan pipelines, as code
// (research-ts-engine.md P4). One `UnitLedger` per unit under `units/`, written after every
// phase; phases run in `PHASES` order; a unit that stops (skip or failure) still runs `cleanup`
// so the ledger closes truthfully. Model phases are `PhaseRunner`s the caller may inject; the
// defaults here return `skipped` until M4.2 fills them in.

import { cleanEnv } from "./adapters/spawn.ts";
import { readUnit, writeLog, writeUnit } from "./ledger.ts";
import type { EventInput } from "./ledger.ts";
import type { Env } from "./paths.ts";
import { claimPhase } from "./phases/claim.ts";
import { cleanupPhase } from "./phases/cleanup.ts";
import { makeUnit, spawnRunner } from "./phases/common.ts";
import type {
  CommandRunner,
  PhaseCtx,
  PhaseResult,
  PhaseRunner,
  UnitsConfig,
} from "./phases/common.ts";
import { prPhase } from "./phases/pr.ts";
import { pushPhase } from "./phases/push.ts";
import { requestReviewPhase } from "./phases/request-review.ts";
import { worktreePhase } from "./phases/worktree.ts";
import { EMPTY_USAGE, PHASES } from "./types.ts";
import type { Phase, State, UnitLedger, UnitRow, UnitsStep, UnitVerdict } from "./types.ts";

export { makeUnit, parseItems } from "./phases/common.ts";
export type {
  CommandRunner,
  PhaseCtx,
  PhaseResult,
  PhaseRunner,
  UnitsConfig,
} from "./phases/common.ts";

export const MODEL_PHASES: readonly Phase[] = ["plan", "implement", "review", "fix", "watch"];
export const MODEL_PHASES_PENDING = "model phases arrive in M4.2";
export const DEFAULT_REVIEWERS = ["copilot-pull-request-reviewer"];

const modelStub: PhaseRunner = () =>
  Promise.resolve({ ok: false, verdict: "skipped", reason: MODEL_PHASES_PENDING });

/** The built-in runner table: no-model phases as code, model phases stubbed. */
export const DEFAULT_RUNNERS: Readonly<Record<Phase, PhaseRunner>> = {
  claim: claimPhase,
  worktree: worktreePhase,
  plan: modelStub,
  implement: modelStub,
  review: modelStub,
  fix: modelStub,
  push: pushPhase,
  pr: prPhase,
  "request-review": requestReviewPhase,
  watch: modelStub,
  cleanup: cleanupPhase,
};

export type UnitsStepInput = {
  runDir: string;
  /** The base repository. */
  cwd: string;
  stepRunId: string;
  /** The rendered step. */
  step: UnitsStep;
  /** Parsed item refs (`parseItems`). */
  items: string[];
  state: State;
  parentEnv?: Env;
  exec?: CommandRunner;
  /** Overrides for any phase (tests, and the M4.2 model runners). */
  runners?: Partial<Record<Phase, PhaseRunner>>;
  emit: (ev: Omit<EventInput, "run_id">) => void;
  signal?: AbortSignal;
};

export type UnitsStepResult = {
  verdict: string;
  outputs: { units: UnitRow[] };
  /** Path of the human-readable step log. */
  log: string;
};

export function unitRow(l: UnitLedger): UnitRow {
  const row: UnitRow = { unit: l.unit, cleaned: l.cleaned };
  if (l.verdict !== undefined) row.verdict = l.verdict;
  if (l.reason !== undefined) row.reason = l.reason;
  if (l.review !== undefined) row.review = l.review;
  return row;
}

/** A unit is done once `cleanup` ran (any verdict) or its worktree was cleaned. */
export function isDone(l: UnitLedger): boolean {
  return l.cleaned || (l.last_phase === "cleanup" && l.verdict !== undefined);
}

export function configFor(step: UnitsStep, state: State): UnitsConfig {
  const caps: Record<string, number> = {};
  for (const name of step.caps ?? []) {
    const v = state.caps[name];
    if (v !== undefined) caps[name] = v;
  }
  return {
    pipeline: step.pipeline,
    reviewers: step.reviewers ?? DEFAULT_REVIEWERS,
    tickets: state.context.ticket ?? [],
    caps,
    groups: step.groups,
  };
}

function applyPatch(ledger: UnitLedger, patch: Partial<UnitLedger>): void {
  for (const [k, v] of Object.entries(patch) as [keyof UnitLedger, unknown][]) {
    if (v === undefined) continue;
    if (k === "cursors") ledger.cursors = { ...ledger.cursors, ...(v as UnitLedger["cursors"]) };
    else if (k === "unit") ledger.unit = { ...ledger.unit, ...(v as UnitLedger["unit"]) };
    else (ledger as unknown as Record<string, unknown>)[k] = v;
  }
}

/** Phases already completed in an earlier attempt are skipped; claim and worktree always re-run. */
function shouldRun(phase: Phase, resumeFrom: UnitLedger | null): boolean {
  if (resumeFrom === null || phase === "claim" || phase === "worktree") return true;
  return PHASES.indexOf(phase) > PHASES.indexOf(resumeFrom.last_phase);
}

/**
 * Units share one `.git` (worktrees), and concurrent `worktree add` / `fetch` / `branch -D` race
 * on its lock files. Serialise `git` per step; `gh` and the model phases stay concurrent.
 */
function serializeGit(exec: CommandRunner): CommandRunner {
  let tail: Promise<unknown> = Promise.resolve();
  return (cmd, args, opts) => {
    if (cmd !== "git") return exec(cmd, args, opts);
    const next = tail.then(() => exec(cmd, args, opts));
    tail = next.catch(() => undefined);
    return next;
  };
}

function summarize(rows: UnitRow[]): string {
  const count = (pred: (v: UnitVerdict | undefined) => boolean): number =>
    rows.filter((r) => pred(r.verdict)).length;
  const merged = count((v) => v === "merged");
  const open = count(
    (v) => v === "all-green" || v === "blocked" || v === "partial" || v === "human-intervention",
  );
  const failed = count((v) => v === "failed" || v === "exhausted" || v === undefined);
  const skipped = count((v) => v === "skipped");
  return `units=${rows.length} merged=${merged} open=${open} failed=${failed} skipped=${skipped}`;
}

export async function runUnitsStep(input: UnitsStepInput): Promise<UnitsStepResult> {
  const { runDir, cwd, step, state } = input;
  const config = configFor(step, state);
  const exec = serializeGit(input.exec ?? spawnRunner);
  const env = cleanEnv({
    ...(input.parentEnv ? { parent: input.parentEnv } : {}),
    extra: { GIT_TERMINAL_PROMPT: "0", GH_PROMPT_DISABLED: "1", GH_NO_UPDATE_NOTIFIER: "1" },
  });
  const runners: Record<Phase, PhaseRunner> = { ...DEFAULT_RUNNERS, ...input.runners };
  const lines: string[] = [];
  const flushLog = (): string =>
    writeLog(runDir, step.id, input.stepRunId, lines.join("\n") + "\n");

  async function processUnit(item: string): Promise<UnitRow> {
    const unit = makeUnit(config.pipeline, item, cwd, runDir, config.base ?? "");
    const log = (line: string): void => {
      lines.push(`[${unit.ref}] ${line}`);
    };
    const existing = readUnit(runDir, unit.branch);
    if (existing && isDone(existing)) {
      log(`already ${existing.verdict ?? "done"}; skipped (resume)`);
      input.emit({
        type: "unit.done",
        unit: unit.ref,
        verdict: existing.verdict ?? "failed",
        message: existing.reason ?? "already done",
      });
      return unitRow(existing);
    }
    const ledger: UnitLedger = existing ?? {
      unit,
      last_phase: "claim",
      cleaned: false,
      cursors: {},
      usage: EMPTY_USAGE(),
      caps: config.caps,
    };
    if (existing) log(`resume from ${existing.last_phase}`);
    let stopped = false;
    for (const phase of PHASES) {
      if (input.signal?.aborted) break;
      if (stopped && phase !== "cleanup") continue;
      if (!shouldRun(phase, existing)) continue;
      input.emit({ type: "unit.phase", unit: unit.ref, phase });
      const ctx: PhaseCtx = { unit: ledger.unit, ledger, cwd, runDir, env, exec, config, log };
      if (input.signal) ctx.signal = input.signal;
      let res: PhaseResult;
      try {
        res = await runners[phase](ctx);
      } catch (err) {
        res = { ok: false, reason: `${phase}: ${(err as Error).message}` };
      }
      if (res.patch) applyPatch(ledger, res.patch);
      ledger.last_phase = phase;
      if (!res.ok) {
        ledger.verdict = res.verdict ?? "failed";
        ledger.reason = res.reason;
        stopped = true;
        log(`${phase}: ${ledger.verdict} (${res.reason})`);
      }
      writeUnit(runDir, unit.branch, ledger);
    }
    if (ledger.verdict === undefined) {
      ledger.verdict = "failed";
      ledger.reason = "no verdict recorded";
      writeUnit(runDir, unit.branch, ledger);
    }
    const ev: Omit<EventInput, "run_id"> = {
      type: "unit.done",
      unit: unit.ref,
      verdict: ledger.verdict,
    };
    if (ledger.reason !== undefined) ev.message = ledger.reason;
    input.emit(ev);
    flushLog();
    return unitRow(ledger);
  }

  const rows: UnitRow[] = [];
  const queue = [...input.items];
  const workers = Math.max(1, Math.min(step.parallel ?? 1, queue.length));
  const worker = async (): Promise<void> => {
    for (;;) {
      if (input.signal?.aborted) return;
      const item = queue.shift();
      if (item === undefined) return;
      rows.push(await processUnit(item));
    }
  };
  await Promise.all(Array.from({ length: workers }, worker));
  // Report rows in item order regardless of which worker finished first.
  const order = new Map(
    input.items.map((item, i) => [makeUnit(config.pipeline, item, cwd, runDir).branch, i]),
  );
  rows.sort((a, b) => (order.get(a.unit.branch) ?? 0) - (order.get(b.unit.branch) ?? 0));
  const log = flushLog();
  return { verdict: summarize(rows), outputs: { units: rows }, log };
}
