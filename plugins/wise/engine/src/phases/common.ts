// Shared contract for the unit phases (research-ts-engine.md P4): the phase context, the
// result shape, the injectable command runner every phase goes through, and the branch /
// worktree naming rules carried over from references/branch-naming.md and process-plans.md.

import { basename, isAbsolute, join, resolve } from "node:path";
import { spawnClean } from "../adapters/spawn.ts";
import type { AgentHandle, AgentStarter, ChannelConfig } from "../steps/agent.ts";
import type {
  ContextTicket,
  Harness,
  McpPolicy,
  Permissions,
  ProfileLevel,
  Resolved,
  RunMode,
  Unit,
  UnitLedger,
  UnitVerdict,
  Usage,
} from "../types.ts";
import type { ModelPhase } from "../prompts/units/schemas.ts";

// ---- command runner -----------------------------------------------------------------------

export type ExecResult = {
  code: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
  /** Set when the process could not be started (ENOENT, EACCES). */
  error?: string;
};
export type ExecOpts = {
  cwd: string;
  env: Record<string, string>;
  /** Wall clock; default `DEFAULT_CMD_TIMEOUT_MS`. */
  timeoutMs?: number;
  signal?: AbortSignal;
};
/** Runs one command and captures its output. Injectable so tests never touch a real remote. */
export type CommandRunner = (
  cmd: string,
  args: readonly string[],
  opts: ExecOpts,
) => Promise<ExecResult>;

export const DEFAULT_CMD_TIMEOUT_MS = 120_000;
/** Network-bound git commands (fetch, push) get longer. */
export const NETWORK_CMD_TIMEOUT_MS = 300_000;
const STDOUT_CAP = 1024 * 1024;

/** The default runner: `spawnClean` with captured stdout, capped, killed on timeout or abort. */
export const spawnRunner: CommandRunner = (cmd, args, opts) => {
  const proc = spawnClean(cmd, args, {
    cwd: opts.cwd,
    env: opts.env,
    timeoutMs: opts.timeoutMs ?? DEFAULT_CMD_TIMEOUT_MS,
  });
  let stdout = "";
  proc.stdout.on("data", (chunk: string) => {
    if (stdout.length < STDOUT_CAP) stdout += chunk.slice(0, STDOUT_CAP - stdout.length);
  });
  proc.stdin.end();
  const onAbort = (): void => proc.kill("SIGTERM");
  opts.signal?.addEventListener("abort", onAbort, { once: true });
  return proc.exited.then((exit): ExecResult => {
    opts.signal?.removeEventListener("abort", onAbort);
    const out: ExecResult = {
      code: exit.code,
      stdout,
      stderr: exit.stderr,
      timedOut: exit.timedOut,
    };
    if (exit.error !== undefined) out.error = exit.error;
    return out;
  });
};

export const ok = (r: ExecResult): boolean => r.code === 0 && !r.timedOut && r.error === undefined;

/** One-line failure text: spawn error, timeout, or the head of stderr (git puts hints after it). */
export function errText(r: ExecResult, max = 300): string {
  if (r.error !== undefined) return `spawn failed: ${r.error}`;
  if (r.timedOut) return "timed out";
  const text = (r.stderr.trim() || r.stdout.trim()).replaceAll(/\s+/g, " ");
  return text.length > max ? text.slice(0, max - 1) + "…" : text || `exit code ${String(r.code)}`;
}

// ---- phase contract ------------------------------------------------------------------------

/** Step-level settings every phase reads. */
export type UnitsConfig = {
  pipeline: "ticket" | "plan";
  /** GitHub logins for `request-review`; empty list disables the phase. */
  reviewers: string[];
  /** Base branch pin; resolved live from `gh` / `origin/HEAD` when absent. */
  base?: string;
  /** Tickets the run's context carries (titles, urls) for PR titles and bodies. */
  tickets: ContextTicket[];
  /** Cap values resolved from the profile (`max_fix_attempts`, `max_review_cycles`, ...). */
  caps: Record<string, number>;
  /** Phase → tuning group id, for the model phases (M4.2). */
  groups: Record<string, string>;
  /** Run profile; the review gate's effort and the low-profile Opus rule read it. */
  profile: ProfileLevel;
  /** `full`: every model phase runs `full-access` instead of its own `PHASE_MODE`. */
  permissions?: Permissions;
  /** Per-provider permission floors chosen during pre-flight. */
  provider_permissions?: Partial<Record<Harness, RunMode>>;
  /** Operator standing guidance from the run context (E1), injected into every model prompt. */
  guidance?: string;
  /** Decisions the conversation already made, injected into the plan prompt. */
  decisions?: Record<string, string>;
  /** E8: `unit` lets the fixer resume the reviewer's session; `fresh` (default) starts clean. */
  resume: "unit" | "fresh";
  /** Which MCP servers every model child loads (`mcp` on the units step); absent = inherit. */
  mcp?: McpPolicy;
  /** Step-level wall clock override for every model child, seconds. */
  timeout?: number;
  max_turns?: number;
};

// ---- model-phase runtime (M4.2) ------------------------------------------------------------------

/** What a model phase needs from the executor to spawn a child through `startAgentStep`. */
export type AgentRuntime = {
  starter: AgentStarter;
  stepId: string;
  stepRunId: string;
  stepToken: string;
  channel?: ChannelConfig;
  /** Wait for a harness slot under the P5 caps; resolves with the release function. */
  acquire?: (harness: Harness, signal?: AbortSignal) => Promise<() => void>;
  /** Register a live child so `cancel` can kill it; returns the unregister function. */
  track?: (key: string, handle: AgentHandle) => () => void;
  /** Wall clock for a child when neither the step nor the phase table sets one. */
  defaultTimeoutMs?: number;
};

export type FixSource = "review" | "ci" | "bot-reviews";
/** Per-call parameters the loops in `units.ts` hand to the `fix` runner. */
export type FixRequest = { source: FixSource; findings_path: string; cursor?: unknown };
/** Per-call parameters for the `review` runner: the pre-push panel or the watch substitute. */
export type ReviewRequest = { shape: "panel" | "universal"; cycle: number };
/** Per-call parameters for one `watch` pass. */
export type WatchRequest = { pass: number; head_sha: string; run_started: string };

export type PhaseCtx = {
  unit: Unit;
  ledger: UnitLedger;
  /** The base repository (the run's cwd). */
  cwd: string;
  runDir: string;
  /** Child environment, already cleaned (P6). */
  env: Record<string, string>;
  exec: CommandRunner;
  config: UnitsConfig;
  /** Human-readable trace line for the step log. */
  log: (line: string) => void;
  /** Apply a ledger patch and persist it now (loops checkpoint between children). */
  checkpoint: (patch: Partial<UnitLedger>) => void;
  /** Injectable sleep for the watch loop; rejects on abort. */
  sleep: (ms: number) => Promise<void>;
  /** Wall clock in ms (injectable for the watch-minutes cap). */
  now: () => number;
  /** Harness/model/effort per model phase, resolved by the executor at run start. */
  resolved: Partial<Record<ModelPhase, Resolved>>;
  /** Absent when no starter was configured: model phases then report `skipped`. */
  agent?: AgentRuntime;
  fix?: FixRequest;
  review?: ReviewRequest;
  watch?: WatchRequest;
  signal?: AbortSignal;
};

/** Extra facts a phase hands back: the child's parsed schema output and its usage. */
export type PhaseExtra = {
  patch?: Partial<UnitLedger>;
  output?: unknown;
  usage?: Usage;
  resolved?: Resolved;
};
export type PhaseResult =
  | ({ ok: true } & PhaseExtra)
  | ({ ok: false; reason: string; verdict?: UnitVerdict } & PhaseExtra);
export type PhaseRunner = (ctx: PhaseCtx) => Promise<PhaseResult>;

export const fail = (
  reason: string,
  verdict?: UnitVerdict,
  patch?: Partial<UnitLedger>,
  extra: Omit<PhaseExtra, "patch"> = {},
): PhaseResult => ({
  ok: false,
  reason,
  ...(verdict !== undefined ? { verdict } : {}),
  ...(patch !== undefined ? { patch } : {}),
  ...extra,
});
export const pass = (
  patch?: Partial<UnitLedger>,
  extra: Omit<PhaseExtra, "patch"> = {},
): PhaseResult => ({
  ok: true,
  ...(patch !== undefined ? { patch } : {}),
  ...extra,
});

/** Run a command through the phase's runner, logging argv and exit. */
export async function run(
  ctx: PhaseCtx,
  cmd: string,
  args: readonly string[],
  opts: { cwd?: string; timeoutMs?: number } = {},
): Promise<ExecResult> {
  const execOpts: ExecOpts = { cwd: opts.cwd ?? ctx.cwd, env: ctx.env };
  if (opts.timeoutMs !== undefined) execOpts.timeoutMs = opts.timeoutMs;
  if (ctx.signal) execOpts.signal = ctx.signal;
  const res = await ctx.exec(cmd, args, execOpts);
  const status = ok(res) ? "ok" : errText(res, 120);
  ctx.log(`$ ${cmd} ${args.join(" ")} -> ${status}`);
  return res;
}

export const git = (
  ctx: PhaseCtx,
  args: readonly string[],
  opts?: { cwd?: string; timeoutMs?: number },
) => run(ctx, "git", args, opts);
export const gh = (
  ctx: PhaseCtx,
  args: readonly string[],
  opts?: { cwd?: string; timeoutMs?: number },
) => run(ctx, "gh", args, opts);

/** Parse a command's stdout as JSON; `undefined` on failure or non-zero exit. */
export function jsonOf(r: ExecResult): unknown {
  if (!ok(r)) return undefined;
  try {
    return JSON.parse(r.stdout) as unknown;
  } catch {
    return undefined;
  }
}

// ---- branches ---------------------------------------------------------------------------------

/** Branches no unit may push to or open a PR from (the plugin's protected-branch rule). */
export function isProtectedBranch(name: string): boolean {
  return name === "main" || name === "master" || name.startsWith("release");
}

/** Does the local repo have `refs/heads/<branch>`? */
export async function localBranchExists(ctx: PhaseCtx, branch: string): Promise<boolean> {
  return ok(await git(ctx, ["show-ref", "--verify", "--quiet", `refs/heads/${branch}`]));
}

/** Does `origin` have `refs/heads/<branch>`? `undefined` when the remote could not be reached. */
export async function remoteBranchExists(
  ctx: PhaseCtx,
  branch: string,
): Promise<boolean | undefined> {
  const r = await git(ctx, ["ls-remote", "--heads", "origin", branch], {
    timeoutMs: NETWORK_CMD_TIMEOUT_MS,
  });
  if (!ok(r)) return undefined;
  return r.stdout.trim().length > 0;
}

/** Base branch: the config pin, then `gh repo view`, then `origin/HEAD`, then `main`. */
export async function resolveBase(ctx: PhaseCtx): Promise<string> {
  if (ctx.config.base) return ctx.config.base;
  const viaGh = await gh(ctx, ["repo", "view", "--json", "defaultBranchRef"]);
  const parsed = jsonOf(viaGh) as { defaultBranchRef?: { name?: unknown } } | undefined;
  const name = parsed?.defaultBranchRef?.name;
  if (typeof name === "string" && name.length > 0) return name;
  const viaGit = await git(ctx, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]);
  if (ok(viaGit)) {
    const short = viaGit.stdout.trim().replace(/^origin\//, "");
    if (short.length > 0) return short;
  }
  return "main";
}

// ---- naming (branch-naming.md, process-plans.md §1) -------------------------------------------------

const ACRONYM_RE = /^[A-Za-z][A-Za-z0-9]*-\d+$/;
const DIGITS_RE = /^\d+$/;

/** Make `s` a legal flat git ref segment: disallowed chars to `-`, no `/`, no `..`, no `.lock`. */
export function sanitizeRef(s: string): string {
  let out = s.replaceAll(/[^A-Za-z0-9._-]+/g, "-").replaceAll(/\.{2,}/g, "-");
  out = out.replaceAll(/^[-.]+|[-.]+$/g, "");
  if (out.endsWith(".lock")) out = out.slice(0, -".lock".length);
  return out;
}

/** Normalise a ticket item: URL → last path segment that looks like a key; strip `#`; trim. */
export function ticketRef(item: string): string {
  let s = item.trim();
  if (/^[a-z]+:\/\//i.test(s) || s.includes("/")) {
    const parts = s.split(/[/?#]/).filter((p) => p.length > 0);
    const key = parts.toReversed().find((p) => ACRONYM_RE.test(p) || DIGITS_RE.test(p));
    s = key ?? parts.at(-1) ?? s;
  }
  return s.replace(/^#/, "").trim();
}

/** The ticket branch: the ref verbatim, or `abstract-task-<n>` for a bare number. */
export function ticketBranch(ref: string): string {
  if (ACRONYM_RE.test(ref)) return ref;
  if (DIGITS_RE.test(ref)) return `abstract-task-${ref}`;
  const clean = sanitizeRef(ref);
  return clean.length > 0 ? clean : "abstract-task-0";
}

/** The plan branch: basename without `.md`, leading `PLAN-` stripped, sanitised; digits → `plan-<n>`. */
export function planBranch(planPath: string): string {
  let slug = basename(planPath)
    .replace(/\.md$/i, "")
    .replace(/^PLAN-/, "");
  slug = sanitizeRef(slug);
  if (slug.length === 0 || DIGITS_RE.test(slug)) return `plan-${slug || "0"}`;
  return slug;
}

/** Worktree directory name under `<runDir>/worktrees/`: the branch, made filesystem-safe. */
export function worktreeSlug(branch: string): string {
  return sanitizeRef(branch) || "unit";
}

/** Build the `Unit` for one item. Plan paths resolve against `cwd`. `base` is set by `claim`. */
export function makeUnit(
  pipeline: "ticket" | "plan",
  item: string,
  cwd: string,
  runDir: string,
  base = "",
): Unit {
  if (pipeline === "plan") {
    const planPath = isAbsolute(item.trim()) ? item.trim() : resolve(cwd, item.trim());
    const branch = planBranch(planPath);
    return {
      ref: branch,
      branch,
      worktree: join(runDir, "worktrees", worktreeSlug(branch)),
      base,
      plan_path: planPath,
    };
  }
  const ref = ticketRef(item);
  const branch = ticketBranch(ref);
  return { ref, branch, worktree: join(runDir, "worktrees", worktreeSlug(branch)), base };
}

/** Items: a JSON array of strings (or `{ref}` objects), else comma / semicolon / newline separated. */
export function parseItems(text: string): string[] {
  const trimmed = text.trim();
  let raw: string[] = [];
  if (trimmed.startsWith("[")) {
    try {
      const arr = JSON.parse(trimmed) as unknown;
      if (Array.isArray(arr)) {
        raw = arr.flatMap((v) => {
          if (typeof v === "string") return [v];
          if (
            typeof v === "object" &&
            v !== null &&
            typeof (v as { ref?: unknown }).ref === "string"
          )
            return [(v as { ref: string }).ref];
          return [];
        });
      }
    } catch {
      raw = [];
    }
  }
  if (raw.length === 0) raw = trimmed.split(/[,;\n]/);
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of raw) {
    const s = item.trim();
    if (s.length === 0 || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}
