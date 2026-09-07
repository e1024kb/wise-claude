// `units` step (plan M4.1 + M4.2): the per-unit loop for the ticket and plan pipelines, as code
// (research-ts-engine.md P4). One `UnitLedger` per unit under `units/`, written after every
// phase and every loop cycle; phases run in `PHASES` order; a unit that stops (skip or failure)
// still runs `cleanup` so the ledger closes truthfully. The model phases spawn one harness child
// each through `phases/model.ts`; this file owns the review ↔ fix cycle, the watch loop, the
// caps, and the merge decision.

import { cleanEnv } from "./adapters/spawn.ts";
import { addUsage, readUnit, utcNow, writeLog, writeUnit } from "./ledger.ts";
import { priceUsage } from "./pricing.ts";
import type { EventInput } from "./ledger.ts";
import type { Env } from "./paths.ts";
import { claimPhase } from "./phases/claim.ts";
import { cleanupPhase } from "./phases/cleanup.ts";
import { fail, makeUnit, pass, spawnRunner } from "./phases/common.ts";
import type {
  AgentRuntime,
  CommandRunner,
  FixSource,
  PhaseCtx,
  PhaseResult,
  PhaseRunner,
  UnitsConfig,
} from "./phases/common.ts";
import {
  findingsPath,
  fixPhase,
  headSha,
  implementPhase,
  mergePr,
  planPhase,
  resolvedFor,
  reviewPhase,
  watchPhase,
} from "./phases/model.ts";
import { prPhase } from "./phases/pr.ts";
import { pushPhase } from "./phases/push.ts";
import { requestReviewPhase } from "./phases/request-review.ts";
import { worktreePhase } from "./phases/worktree.ts";
import {
  isModelPhase,
  MODEL_PHASES,
  parseFix,
  parseReview,
  parseWatch,
  phaseKey,
} from "./prompts/units/schemas.ts";
import type { ModelPhase, WatchOutput } from "./prompts/units/schemas.ts";
import { EMPTY_USAGE, PHASES } from "./types.ts";
import type {
  Harness,
  Phase,
  Resolved,
  State,
  UnitLedger,
  UnitRow,
  UnitsStep,
  UnitVerdict,
  Usage,
} from "./types.ts";

export { makeUnit, parseItems } from "./phases/common.ts";
export type {
  AgentRuntime,
  CommandRunner,
  PhaseCtx,
  PhaseResult,
  PhaseRunner,
  UnitsConfig,
} from "./phases/common.ts";
export { NO_AGENT_RUNTIME, resolveUnitPhases } from "./phases/model.ts";
export { MODEL_PHASES, phaseKey } from "./prompts/units/schemas.ts";
export type { ModelPhase } from "./prompts/units/schemas.ts";

export const DEFAULT_REVIEWERS = ["copilot-pull-request-reviewer"];

/** Cap defaults when the profile sets none (P4, E11). Names are the YAML `caps:` names. */
export const CAP_DEFAULTS = {
  max_review_cycles: 2,
  max_fix_attempts: 3,
  watch_minutes: 45,
  watch_poll_seconds: 60,
  watch_stable_passes: 2,
} as const;
export type CapName = keyof typeof CAP_DEFAULTS;

/** The built-in runner table: no-model phases as code, model phases over one child each. */
export const DEFAULT_RUNNERS: Readonly<Record<Phase, PhaseRunner>> = {
  claim: claimPhase,
  worktree: worktreePhase,
  plan: planPhase,
  implement: implementPhase,
  review: reviewPhase,
  fix: fixPhase,
  push: pushPhase,
  pr: prPhase,
  "request-review": requestReviewPhase,
  watch: watchPhase,
  cleanup: cleanupPhase,
};

/** What the executor hands the model phases; `stepId` / `stepRunId` come from the step itself. */
export type UnitsAgentInput = Omit<AgentRuntime, "stepId" | "stepRunId">;

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
  /** Overrides for any phase (tests). */
  runners?: Partial<Record<Phase, PhaseRunner>>;
  /** Model-phase runtime; absent = the model phases report `skipped`. */
  agent?: UnitsAgentInput;
  /** Every model child's usage (priced, M6.1) after it was folded into the unit ledger. */
  onUsage?: (phase: Phase, harness: Harness, usage: Usage, model: string) => void;
  /** Watch-loop sleep; the default resolves early on abort. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  now?: () => number;
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
  const cfg: UnitsConfig = {
    pipeline: step.pipeline,
    reviewers: step.reviewers ?? DEFAULT_REVIEWERS,
    tickets: state.context.ticket ?? [],
    caps,
    groups: step.groups,
    profile: state.profile,
    resume: step.resume ?? "fresh",
  };
  if (state.permissions !== undefined) cfg.permissions = state.permissions;
  if (state.context.guidance !== undefined) cfg.guidance = state.context.guidance;
  if (state.context.decisions !== undefined) cfg.decisions = state.context.decisions;
  if (step.mcp !== undefined) cfg.mcp = step.mcp;
  if (step.timeout !== undefined) cfg.timeout = step.timeout;
  if (step.max_turns !== undefined) cfg.max_turns = step.max_turns;
  return cfg;
}

/** A cap from the profile, else its default. */
export function capOf(caps: Record<string, number>, name: CapName): number {
  const v = caps[name];
  return v !== undefined && v >= 0 ? v : CAP_DEFAULTS[name];
}

/** The executor's per-phase resolution for this step (`<step>.<phase>` keys in `state.resolved`). */
export function resolvedPhases(
  step: UnitsStep,
  state: State,
): Partial<Record<ModelPhase, Resolved>> {
  const out: Partial<Record<ModelPhase, Resolved>> = {};
  for (const phase of MODEL_PHASES) {
    const r = state.resolved[phaseKey(step.id, phase)];
    if (r) out[phase] = r;
  }
  return out;
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

function defaultSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (ms <= 0 || signal?.aborted) {
      resolve();
      return;
    }
    const done = (): void => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    timer.unref?.();
    signal?.addEventListener("abort", done, { once: true });
  });
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

// ---- loops -------------------------------------------------------------------------------------------

/** What a loop needs from the unit driver: phase events and per-child bookkeeping. */
type Hooks = {
  emitPhase: (phase: Phase) => void;
  /** Fold usage and cursors of one child result into the ledger and persist it. */
  fold: (phase: Phase, res: PhaseResult) => void;
};
type Runners = Readonly<Record<Phase, PhaseRunner>>;

/**
 * Review, then fix and review again while the reviewer requests changes, up to
 * `max_review_cycles` reviews. Non-convergence is recorded and the unit still pushes.
 */
export async function reviewFixLoop(
  ctx: PhaseCtx,
  runners: Runners,
  hooks: Hooks,
): Promise<PhaseResult> {
  const max = Math.max(1, capOf(ctx.config.caps, "max_review_cycles"));
  let cycles = 0;
  let converged = false;
  for (;;) {
    cycles++;
    hooks.emitPhase("review");
    const r = await runners.review({ ...ctx, review: { shape: "panel", cycle: cycles } });
    hooks.fold("review", r);
    if (!r.ok) return r;
    const out = parseReview(r.output);
    if (!out) return fail("review: no structured output");
    if (out.verdict === "approve") {
      converged = true;
      break;
    }
    if (cycles >= max) break;
    hooks.emitPhase("fix");
    const cursor = ctx.config.resume === "unit" ? ctx.ledger.cursors.review : undefined;
    const f = await runners.fix({
      ...ctx,
      fix: {
        source: "review",
        findings_path: findingsPath(ctx),
        ...(cursor !== undefined ? { cursor } : {}),
      },
    });
    hooks.fold("fix", f);
    if (!f.ok) return f;
    ctx.checkpoint({ review: { converged: false, cycles } });
  }
  ctx.log(
    converged
      ? `review: converged after ${cycles} cycle(s)`
      : `review: not converged after ${cycles} cycle(s); pushing anyway`,
  );
  return pass({ review: { converged, cycles } });
}

/**
 * Poll the PR with one `watch` child per pass. Red CI or open bot comments go to `fix` then
 * `push` (each commit-producing round counts against `max_fix_attempts`); a stuck bot gets the
 * substitute review once per head; a human comment stands the loop down; `watch_stable_passes`
 * consecutive green-and-resolved passes merge (squash, then merge commit).
 */
export async function watchLoop(
  ctx: PhaseCtx,
  runners: Runners,
  hooks: Hooks,
): Promise<PhaseResult> {
  if (!ctx.unit.pr) return fail("watch: no PR recorded");
  const { caps } = ctx.config;
  const maxFix = capOf(caps, "max_fix_attempts");
  const minutes = capOf(caps, "watch_minutes");
  const pollMs = capOf(caps, "watch_poll_seconds") * 1000;
  const stableTarget = Math.max(1, capOf(caps, "watch_stable_passes"));
  const w: NonNullable<UnitLedger["watch"]> = ctx.ledger.watch ?? {
    passes: 0,
    fix_attempts: 0,
    stable: 0,
  };
  const save = (): void => ctx.checkpoint({ watch: { ...w } });
  const started = ctx.now();
  const runStarted = utcNow(new Date(started));
  let last: WatchOutput | undefined;
  hooks.emitPhase("watch");

  const fixAndPush = async (source: FixSource): Promise<PhaseResult> => {
    if (w.fix_attempts >= maxFix) {
      return fail(
        `max_fix_attempts (${maxFix}) reached; ${source} still needs a fix`,
        "exhausted",
        { watch: { ...w } },
      );
    }
    hooks.emitPhase("fix");
    const f = await runners.fix({ ...ctx, fix: { source, findings_path: findingsPath(ctx) } });
    hooks.fold("fix", f);
    if (!f.ok) return f;
    const out = parseFix(f.output);
    if (!out || out.commits === 0) {
      return fail(`fix produced no commit for ${source}`, "partial", { watch: { ...w } });
    }
    w.fix_attempts++;
    w.stable = 0;
    save();
    hooks.emitPhase("push");
    const p = await runners.push(ctx);
    hooks.fold("push", p);
    return p;
  };

  for (;;) {
    if (ctx.signal?.aborted) return fail("watch: cancelled");
    if (ctx.now() - started >= minutes * 60_000) {
      const detail = last ? ` (ci=${last.ci}, bots=${last.bot_reviews})` : "";
      return fail(
        `watch_minutes (${minutes}) cap reached${detail}`,
        last?.ci === "green" ? "all-green" : "exhausted",
        { watch: { ...w } },
      );
    }
    const head = await headSha(ctx);
    w.passes++;
    const r = await runners.watch({
      ...ctx,
      watch: { pass: w.passes, head_sha: head, run_started: runStarted },
    });
    hooks.fold("watch", r);
    save();
    if (!r.ok) return r;
    const out = parseWatch(r.output);
    if (!out) return fail("watch: no structured output");
    last = out;

    if (out.merged) return pass({ verdict: "merged", watch: { ...w } });
    if (out.human_comment || out.verdict === "needs-human") {
      return fail("a human commented on the PR; standing down", "human-intervention", {
        watch: { ...w },
      });
    }
    if (out.verdict === "blocked") {
      return fail("a bot review item needs a human (see the findings file)", "blocked", {
        watch: { ...w },
      });
    }
    if (out.ci === "red" || out.bot_reviews === "open") {
      const res = await fixAndPush(out.ci === "red" ? "ci" : "bot-reviews");
      if (!res.ok) return res;
      await ctx.sleep(pollMs);
      continue;
    }

    let covered = out.bot_reviews === "resolved";
    if (out.bot_reviews === "stuck") {
      if (w.fallback_sha === head) {
        covered = true;
      } else {
        hooks.emitPhase("review");
        const sub = await runners.review({
          ...ctx,
          review: { shape: "universal", cycle: w.passes },
        });
        hooks.fold("review", sub);
        if (!sub.ok) return fail(`substitute review failed: ${sub.reason}`, "all-green");
        const verdict = parseReview(sub.output)?.verdict;
        if (verdict === "changes-requested") {
          const res = await fixAndPush("review");
          if (!res.ok) return res;
          await ctx.sleep(pollMs);
          continue;
        }
        w.fallback_sha = head;
        covered = true;
        save();
        ctx.log(`watch: substitute review covered ${head.slice(0, 12)}`);
      }
    }

    if (out.ci === "green" && covered) {
      w.stable++;
      save();
      if (w.stable >= stableTarget) {
        const merged = await mergePr(ctx);
        if (!merged.ok) return fail(merged.reason, "all-green", { watch: { ...w } });
        ctx.log(`watch: merged #${ctx.unit.pr.number}`);
        return pass({ verdict: "merged", watch: { ...w } });
      }
    } else if (w.stable !== 0) {
      w.stable = 0;
      save();
    }
    await ctx.sleep(pollMs);
  }
}

// ---- step -------------------------------------------------------------------------------------------

export async function runUnitsStep(input: UnitsStepInput): Promise<UnitsStepResult> {
  const { runDir, cwd, step, state } = input;
  const config = configFor(step, state);
  const resolved = resolvedPhases(step, state);
  const exec = serializeGit(input.exec ?? spawnRunner);
  const env = cleanEnv({
    ...(input.parentEnv ? { parent: input.parentEnv } : {}),
    extra: { GIT_TERMINAL_PROMPT: "0", GH_PROMPT_DISABLED: "1", GH_NO_UPDATE_NOTIFIER: "1" },
  });
  const runners: Record<Phase, PhaseRunner> = { ...DEFAULT_RUNNERS, ...input.runners };
  const agent: AgentRuntime | undefined = input.agent
    ? { ...input.agent, stepId: step.id, stepRunId: input.stepRunId }
    : undefined;
  const sleep = input.sleep ?? defaultSleep;
  const now = input.now ?? Date.now;
  const lines: string[] = [];
  // Models with no price row, logged once per step (M6.1).
  const pricingWarned = new Set<string>();
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
    const persist = (): void => writeUnit(runDir, unit.branch, ledger);
    const checkpoint = (patch: Partial<UnitLedger>): void => {
      applyPatch(ledger, patch);
      persist();
    };
    const makeCtx = (): PhaseCtx => {
      const ctx: PhaseCtx = {
        unit: ledger.unit,
        ledger,
        cwd,
        runDir,
        env,
        exec,
        config,
        log,
        checkpoint,
        sleep: (ms) => sleep(ms, input.signal),
        now,
        resolved,
      };
      if (agent) ctx.agent = agent;
      if (input.signal) ctx.signal = input.signal;
      return ctx;
    };
    const hooks: Hooks = {
      emitPhase: (phase) => {
        const ev: Omit<EventInput, "run_id"> = { type: "unit.phase", unit: unit.ref, phase };
        if (isModelPhase(phase) && agent) {
          const r = resolvedFor(makeCtx(), phase);
          ev.harness = r.harness;
          ev.model = r.model;
          if (r.effort !== "") ev.effort = r.effort;
        }
        input.emit(ev);
      },
      fold: (phase, res) => {
        if (res.patch) applyPatch(ledger, res.patch);
        if (res.usage) {
          // Priced here so the unit ledger and the run views carry the same figure; the
          // executor's fold sees `cost_source` set and does not price again.
          const harness = res.resolved?.harness ?? "claude";
          const model = res.resolved?.model ?? "inherit";
          const priced = priceUsage(res.usage, harness, model);
          if (priced.unknownModel !== undefined && !pricingWarned.has(priced.unknownModel)) {
            pricingWarned.add(priced.unknownModel);
            log(
              `${phase}: no price for ${harness} model ${priced.unknownModel}; api-key cost not counted`,
            );
          }
          addUsage(ledger.usage, priced.usage);
          ledger.usage_by_phase ??= {};
          addUsage((ledger.usage_by_phase[phase] ??= EMPTY_USAGE(priced.usage.pool)), priced.usage);
          input.onUsage?.(phase, harness, priced.usage, model);
        }
        persist();
      },
    };

    let stopped = false;
    for (const phase of PHASES) {
      if (input.signal?.aborted) break;
      if (stopped && phase !== "cleanup") continue;
      if (!shouldRun(phase, existing)) continue;
      // `fix` never runs on its own: the review loop and the watch loop drive it.
      if (phase === "fix") continue;
      const ctx = makeCtx();
      let res: PhaseResult;
      try {
        if (phase === "review") res = await reviewFixLoop(ctx, runners, hooks);
        else if (phase === "watch") res = await watchLoop(ctx, runners, hooks);
        else {
          hooks.emitPhase(phase);
          res = await runners[phase](ctx);
          hooks.fold(phase, res);
        }
      } catch (err) {
        res = { ok: false, reason: `${phase}: ${(err as Error).message}` };
      }
      // Loops folded their children already; their own patch (review, watch, verdict) lands here.
      if ((phase === "review" || phase === "watch") && res.patch) applyPatch(ledger, res.patch);
      ledger.last_phase = phase;
      if (!res.ok) {
        ledger.verdict = res.verdict ?? "failed";
        ledger.reason = res.reason;
        stopped = true;
        log(`${phase}: ${ledger.verdict} (${res.reason})`);
      }
      persist();
    }
    if (ledger.verdict === undefined) {
      ledger.verdict = "failed";
      ledger.reason = "no verdict recorded";
      persist();
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
  // Dedupe by branch, not by raw item: two spellings of one ticket ("ABC-1" and its browse url)
  // map to the same branch, worktree and unit ledger, so a second unit would race the first.
  const seenBranch = new Set<string>();
  const items = input.items.filter((item) => {
    const branch = makeUnit(config.pipeline, item, cwd, runDir).branch;
    if (seenBranch.has(branch)) {
      // The branch is lossy (a plan basename, a sanitised free-text ref), so say which item
      // collapsed into which branch rather than letting it vanish from the row count.
      lines.push(`[${item}] duplicate of an earlier item (branch ${branch}); skipped`);
      return false;
    }
    seenBranch.add(branch);
    return true;
  });
  const queue = [...items];
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
    items.map((item, i) => [makeUnit(config.pipeline, item, cwd, runDir).branch, i]),
  );
  rows.sort((a, b) => (order.get(a.unit.branch) ?? 0) - (order.get(b.unit.branch) ?? 0));
  const log = flushLog();
  return { verdict: summarize(rows), outputs: { units: rows }, log };
}
