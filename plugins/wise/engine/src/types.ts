// Shared engine types. Source: docs/wise/research-ts-engine.md P1 (protocol), P2 (YAML v2),
// P3 (ledger), P4 (units), P6 (adapters). Erasable syntax only: `type` aliases and `as const`.

// ---- vocabulary -----------------------------------------------------------

export const HARNESSES = ["claude", "codex", "gemini", "grok"] as const;
export type Harness = (typeof HARNESSES)[number];

export const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;
export type Effort = (typeof EFFORTS)[number];

export const AUTH_MODES = ["subscription", "api-key"] as const;
export type AuthMode = (typeof AUTH_MODES)[number];

export const RUN_MODES = ["approval-required", "auto", "full-access"] as const;
export type RunMode = (typeof RUN_MODES)[number];

export const PROFILE_LEVELS = ["low", "medium", "max"] as const;
export type ProfileLevel = (typeof PROFILE_LEVELS)[number];

export const STEP_TYPES = ["agent", "bash", "approval", "ask", "units"] as const;
export type StepType = (typeof STEP_TYPES)[number];

export const TRIGGER_RULES = [
  "all-success",
  "one-success",
  "all-done",
  "none-failed",
  "none-failed-min-one-success",
] as const;
export type TriggerRule = (typeof TRIGGER_RULES)[number];

export const STEP_STATUSES = [
  "pending",
  "running",
  "completed",
  "failed",
  "skipped",
  "cancelled",
] as const;
export type StepStatus = (typeof STEP_STATUSES)[number];
export const TERMINAL_STEP: ReadonlySet<StepStatus> = new Set([
  "completed",
  "failed",
  "skipped",
  "cancelled",
]);

export const RUN_STATUSES = [
  "initializing",
  "running",
  "gated",
  "paused",
  "completed",
  "failed",
  "cancelled",
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];
/** Runs that count as history (prunable, not resumable). `failed` stays resumable, as in v1. */
export const TERMINAL_RUN: ReadonlySet<RunStatus> = new Set(["completed", "cancelled"]);

// ---- P1 protocol ------------------------------------------------------------

export type QuestionOption = { value: string; label: string; description?: string };
export type Question = {
  /** "harness.<group>" | "model.<group>" | "effort.<group>" | "step-select" | "input.<name>" */
  id: string;
  kind: "choice" | "multi" | "text";
  label: string;
  options?: QuestionOption[];
  default?: string | string[];
  /** Not asked; shown as fixed. */
  locked?: boolean;
  /** Input may be left empty (inputs only). */
  optional?: boolean;
};
export type Answers = Record<string, string | string[]>;

export type ContextTicket = { ref: string; title?: string; body?: string; url?: string };
export type Context = {
  ticket?: ContextTicket[];
  guidance?: string;
  decisions?: Record<string, string>;
  links?: string[];
};

/**
 * Where a `cost_usd` came from (M6.1): `reported` by the child CLI, `priced` by the engine's
 * table (api-key runs whose child gave tokens only), `none` when no dollar figure exists.
 * Aggregates are `reported` only when every costed part was; one priced part makes them `priced`.
 */
export type CostSource = "reported" | "priced" | "none";

export type Usage = {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  cost_usd?: number;
  pool: AuthMode;
  cost_source?: CostSource;
};
export const EMPTY_USAGE = (pool: AuthMode = "subscription"): Usage => ({
  input: 0,
  output: 0,
  cache_read: 0,
  cache_write: 0,
  pool,
});

export const EVENT_TYPES = [
  "run.started",
  "step.started",
  "step.progress",
  "step.done",
  "unit.phase",
  "unit.done",
  "usage",
  "gate.opened",
  "gate.answered",
  "run.done",
  "run.failed",
  "warn",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

/** What a child may say through `wise_report` (P8). */
export const REPORT_KINDS = ["progress", "blocker", "decision", "finding"] as const;
export type ReportKind = (typeof REPORT_KINDS)[number];

export type Event = {
  seq: number;
  ts: string;
  run_id: string;
  type: EventType;
  step?: string;
  unit?: string;
  phase?: string;
  /** One line, <= 200 chars. */
  verdict?: string;
  outputs?: Record<string, string | number | boolean>;
  usage?: Usage;
  harness?: Harness;
  model?: string;
  effort?: Effort;
  message?: string;
  /** `step.progress` from a child report: the report kind. */
  kind?: ReportKind;
  /** `step.progress` from a child report: small structured payload (<= 1 kB serialized). */
  data?: Record<string, unknown>;
};

/** Live status of one running agent child, derived from its event stream and its reports (P8). */
export type ChildProgress = {
  step: string;
  turn: number;
  tool?: string;
  /** What the last tool touched: a path, a command, a pattern (one line, clipped). */
  detail?: string;
  /** Headline of the child's latest assistant text (clipped). */
  text?: string;
  tokens: number;
  last_activity: string;
  reports: number;
  /** Milliseconds since the tracker (the child) started. */
  elapsed_ms?: number;
};

export type Gate = {
  gate_id: string;
  step: string;
  kind: "approval" | "ask";
  message: string;
  options?: { value: string; label: string }[];
  allow_text?: boolean;
  expires_at?: string;
  /** Present on the per-run token ceiling gate (M6.2): tokens used and the ceiling crossed. */
  ceiling?: { used: number; limit: number };
};

export type Resolved = { harness: Harness; model: string; effort: Effort | ""; reason?: string };

export type RunSummary = {
  run_id: string;
  workflow: string;
  status: RunStatus;
  started_at: string;
  last_activity_at: string;
  completed_at?: string;
  cwd: string;
  gate?: Gate;
  /** Running agent children of a live run (P8); absent when the daemon holds no live children. */
  children?: ChildProgress[];
  /** Single-run status only: every pool folded into one figure (M6.1). */
  usage_total?: Usage;
};

export const ERROR_CODES = [
  "WORKFLOW_NOT_FOUND",
  "WORKFLOW_INVALID",
  "RUN_NOT_FOUND",
  "GATE_STALE",
  "HARNESS_UNAVAILABLE",
  "AUTH_REQUIRED",
  "BUDGET_EXCEEDED",
  "DAEMON_VERSION_MISMATCH",
  "NOT_IMPLEMENTED",
  "ALREADY_RUNNING",
  "TOKEN_INVALID",
  "MISSING_ANSWERS",
  "REQUIRES_MISSING",
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];

// ---- P2 workflow YAML v2 --------------------------------------------------------

export type TuningDefault = { harness?: Harness; model?: string; effort?: Effort };
export type TuningGroup = {
  id: string;
  label?: string;
  description?: string;
  default: TuningDefault;
  fallback?: Harness[];
  locked?: boolean;
  /** Lets the `low` profile run this group's `auth: api-key` steps (M6.2). */
  "allow-api"?: boolean;
  /** Optional preset menu; each preset is a full or partial TuningDefault. */
  options?: { id: string; label?: string; description?: string; value: TuningDefault }[];
};
export type Tuning = { groups: TuningGroup[] };

export type ProfileDef = {
  tuning?: Record<string, TuningDefault>;
  caps?: Record<string, number>;
  description?: string;
};
export type Profiles = Partial<Record<ProfileLevel, ProfileDef>>;

export type InputDef = {
  name: string;
  prompt?: string;
  description?: string;
  optional?: boolean;
  default?: string;
  /** Context path, e.g. `ticket[].ref` or `guidance` (E1). */
  "from-context"?: string;
  /** Regex the raw value must match (v1 `validate:` carried over). */
  validate?: string;
  /** Regex with one capture group extracting the canonical value (v1 `extract:`). */
  extract?: string;
};

export type JsonSchema = Record<string, unknown>;

export type StepOverrides = {
  harness?: Harness;
  model?: string;
  effort?: Effort;
  auth?: AuthMode;
  fallback?: Harness[];
  mode?: RunMode;
  resume?: "unit" | "fresh";
  max_turns?: number;
  /** Wall clock, seconds. */
  timeout?: number;
  /** Idle seconds before the stale policy acts (nudge, then kill); default 600. */
  stale_after?: number;
  /** Harness permission rules granted to the child (Claude `--allowedTools`, e.g. `Bash(git:*)`). */
  allowed_tools?: string[];
  /** Lets the `low` profile run this step under `auth: api-key` (M6.2). */
  "allow-api"?: boolean;
};

export type StepBase = StepOverrides & {
  id: string;
  type: StepType;
  /** Tuning group id binding harness/model/effort. */
  group?: string;
  depends_on?: string[];
  "trigger-rule"?: TriggerRule;
  /** Boolean expression over run outputs, evaluated by `scheduler`. */
  when?: string;
  description?: string;
  optional?: boolean;
};
export type AgentStep = StepBase & {
  type: "agent";
  prompt: string;
  /** Sugar: `skill: wise-commit` emits `prompt: "Run /wise-commit"` with `harness: claude` forced. */
  skill?: string;
  schema?: JsonSchema;
  outputs?: string[];
  /** Deprecated v1 regex; accepted with a warning for one release. */
  until?: string;
};
export type BashStep = StepBase & { type: "bash"; run: string; outputs?: string[] };
export type ApprovalStep = StepBase & { type: "approval"; message: string };
export type AskStep = StepBase & {
  type: "ask";
  message: string;
  options?: string[];
  allow_text?: boolean;
  output?: string;
};
export type UnitsStep = StepBase & {
  type: "units";
  pipeline: "ticket" | "plan";
  items: string;
  groups: Record<string, string>;
  caps?: string[];
  parallel?: number;
  /** GitHub logins to request review from; default `copilot-pull-request-reviewer`. */
  reviewers?: string[];
};
export type Step = AgentStep | BashStep | ApprovalStep | AskStep | UnitsStep;

export type StepSelect = {
  prompt?: string;
  /** Step ids the user may switch off; absent = every non-required step. */
  optional?: string[];
};

export type WorkflowDef = {
  version: 2;
  name: string;
  description?: string;
  "project-selection"?: "current" | "ask" | "none";
  preflight?: {
    "control-mode"?: "synchronous" | "interactive";
    worktree?: "current" | "new";
  };
  requires?: { plugins?: string[]; tools?: string[] };
  tuning?: Tuning;
  profiles?: Profiles;
  inputs?: InputDef[];
  "step-select"?: StepSelect;
  steps: Step[];
};

/** A located definition: folder form (`<dir>/workflow.yaml`) or flat form (`<name>.yaml`). */
export type LocatedDef = {
  name: string;
  path: string;
  /** Directory the prompts resolve against (`workflow.dir`). */
  dir: string;
  source: "user" | "bundled";
};

export type ValidationIssue = {
  level: "error" | "warning";
  path: string;
  message: string;
  /** Present when the issue is a v1 construct with a v2 migration. */
  hint?: string;
};

// ---- P3 ledger ---------------------------------------------------------------

export type StepState = {
  status: StepStatus;
  step_run_id?: string;
  started_at?: string;
  completed_at?: string;
  verdict?: string;
  outputs?: Record<string, unknown>;
  cursor?: unknown;
  attempts: number;
  resolved?: Resolved;
  error?: string;
  /** Every child of this step folded together (a `units` step: all its phases). */
  usage?: Usage;
};

/**
 * Run usage views (E14): per pool, per harness, per step. Every view is fed by one fold
 * (`foldUsageViews` in ledger.ts), so their sums always agree.
 */
export type UsageByPool = Record<AuthMode, Usage> & {
  by_harness: Partial<Record<Harness, Usage>>;
  by_step: Record<string, Usage>;
};

export type Project = { path: string; name: string; kind: string };

export type State = {
  version: 2;
  run_id: string;
  workflow: { name: string; version: number; dir: string };
  cwd: string;
  project: Project | null;
  harness_session?: string;
  status: RunStatus;
  profile: ProfileLevel;
  answers: Answers;
  context: Context;
  inputs: Record<string, string>;
  resolved: Record<string, Resolved>;
  caps: Record<string, number>;
  usage: UsageByPool;
  steps: Record<string, StepState>;
  outputs: Record<string, unknown>;
  gate?: Gate;
  started_at: string;
  last_activity_at: string;
  completed_at?: string;
  error?: string;
};

// ---- P4 units ----------------------------------------------------------------

export type Unit = {
  ref: string;
  branch: string;
  worktree: string;
  base: string;
  plan_path?: string;
  pr?: { number: number; url: string };
};
export const PHASES = [
  "claim",
  "worktree",
  "plan",
  "implement",
  "review",
  "fix",
  "push",
  "pr",
  "request-review",
  "watch",
  "cleanup",
] as const;
export type Phase = (typeof PHASES)[number];
export type UnitVerdict =
  | "merged"
  | "all-green"
  | "blocked"
  | "partial"
  | "exhausted"
  | "human-intervention"
  | "failed"
  /** Not processed: claimed by another run, or a phase not yet implemented. */
  | "skipped";
export type UnitLedger = {
  unit: Unit;
  last_phase: Phase;
  verdict?: UnitVerdict;
  reason?: string;
  review?: { converged: boolean; cycles: number };
  /** Watch-loop counters (M4.2); `fallback_sha` is the head the substitute review covered. */
  watch?: { passes: number; fix_attempts: number; stable: number; fallback_sha?: string };
  cleaned: boolean;
  blueprint?: string;
  /** The plan the implement phase ran: engine-written under `<runDir>/plans/`. */
  plan_path?: string;
  cursors: Partial<Record<Phase, unknown>>;
  usage: Usage;
  /** The unit total split per model phase (M6.1); review / fix accumulate across cycles. */
  usage_by_phase?: Partial<Record<Phase, Usage>>;
  /** Cap values the step resolved from the run's profile (P4), stored for the model phases. */
  caps?: Record<string, number>;
};
export type UnitRow = Pick<UnitLedger, "unit" | "verdict" | "reason" | "review" | "cleaned">;

// ---- P6 adapters ----------------------------------------------------------------

export type RunReq = {
  prompt: string;
  system?: string;
  model: string;
  effort?: Effort;
  schema?: JsonSchema;
  cwd: string;
  mode: RunMode;
  resume?: unknown;
  max_turns?: number;
  timeout_ms: number;
  auth: AuthMode;
  env?: Record<string, string>;
  /** Engine-provided MCP servers for the child (D18: Claude runs with `--strict-mcp-config`). */
  mcp_config?: { mcpServers: Record<string, unknown> };
  /** Extra directories the child may read and write (Claude `--add-dir`); the run dir always. */
  add_dirs?: string[];
  /** Permission rules pre-granted to the child; the engine MCP server is always added. */
  allowed_tools?: string[];
  /** Per-step token the child channel (P8) presents back to the daemon. */
  step_token?: string;
};
export type ExitClass = "ok" | "error" | "rate_limited" | "auth" | "timeout" | "max_turns";
export type RunRes = {
  text: string;
  json?: unknown;
  usage: Usage;
  /** The model the child actually ran when the vendor reports it; pricing prefers it. */
  model?: string;
  cursor?: unknown;
  exit: ExitClass;
  error?: string;
  /** Non-fatal notes: permission denials, ignored cursor shapes. */
  warnings?: string[];
};
export type RawEvent = { ts: string; harness: Harness; line: string; parsed?: unknown };
export type Adapter = {
  id: Harness;
  probeAuth(auth: AuthMode): Promise<{ ok: boolean; login_cmd?: string }>;
  run(req: RunReq, onEvent: (e: RawEvent) => void): Promise<RunRes>;
  effortMap(e: Effort): string | undefined;
};
