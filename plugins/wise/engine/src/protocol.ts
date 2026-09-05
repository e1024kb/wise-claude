// JSON-RPC 2.0 vocabulary of the wise daemon (docs/wise/research-ts-engine.md P1, P5).
// Types only: the wire format is newline-delimited JSON (see rpc.ts), the daemon serves it
// on a Unix socket and the MCP server re-exposes the same methods as tools.

import type {
  Answers,
  Context,
  ErrorCode,
  Event,
  Gate,
  ProfileLevel,
  Question,
  ReportKind,
  Resolved,
  RunStatus,
  RunSummary,
  UnitRow,
  Usage,
  UsageByPool,
} from "./types.ts";

// ---- JSON-RPC 2.0 envelopes ---------------------------------------------------------

export type JsonRpcId = number | string;

export type JsonRpcRequest = {
  jsonrpc: "2.0";
  id: JsonRpcId;
  method: string;
  params?: unknown;
};

/** A request without `id`: fire-and-forget in either direction (server → client progress). */
export type JsonRpcNotification = {
  jsonrpc: "2.0";
  method: string;
  params?: unknown;
};

export type JsonRpcError = {
  code: number;
  message: string;
  data?: unknown;
};

export type JsonRpcSuccess = { jsonrpc: "2.0"; id: JsonRpcId; result: unknown };
export type JsonRpcFailure = { jsonrpc: "2.0"; id: JsonRpcId | null; error: JsonRpcError };
export type JsonRpcResponse = JsonRpcSuccess | JsonRpcFailure;
export type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse;

// Standard JSON-RPC codes.
export const RPC_PARSE_ERROR = -32700;
export const RPC_INVALID_REQUEST = -32600;
export const RPC_METHOD_NOT_FOUND = -32601;
export const RPC_INVALID_PARAMS = -32602;
export const RPC_INTERNAL_ERROR = -32603;
/** Server-defined: a domain error whose `data.code` names the cause (P1 closed list). */
export const RPC_DOMAIN_ERROR = -32000;
// Client-side only, never sent on the wire.
export const RPC_CLIENT_TIMEOUT = -32800;
export const RPC_CLIENT_DISCONNECTED = -32801;

/**
 * Domain error codes. `ErrorCode` is the P1 closed list from types.ts; the two extra literals are
 * daemon-local until they are promoted into `ERROR_CODES` (see the M2.2 report).
 */
export type DomainErrorCode = ErrorCode;

/** `error.data` of a domain error: the code plus free-form detail (e.g. `harness`, `login_cmd`). */
export type DomainErrorData = { code: DomainErrorCode } & Record<string, unknown>;

// ---- methods -------------------------------------------------------------------------

/** First call on every connection; the server refuses anything else (except `shutdown`) before it. */
export type HelloParams = { version: string; client: string };
export type HelloResult = { version: string; pid: number; started_at: string };

/** `idle` (default): exit once no run is active. `now`: exit right after the reply. */
export type ShutdownParams = { when?: "idle" | "now" };
export type ShutdownResult = { accepted: true; active_runs: number };

/** `profile`: the harness session's token-budget level; presets the `profile` question's default. */
export type PreflightParams = { workflow: string; cwd: string; profile?: ProfileLevel };
export type PreflightResult = {
  workflow: string;
  version: number;
  questions: Question[];
  defaults: Answers;
};

export type RunParams = {
  workflow: string;
  cwd: string;
  answers: Answers;
  context: Context;
  inputs: Record<string, string>;
  /** Session profile used when `answers.profile` is absent (the daemon has no session id). */
  profile?: ProfileLevel;
};
export type RunResult = { run_id: string; status: "running" };

export type WaitParams = {
  run_id: string;
  /** Return events with `seq > after`; default 0 (everything). */
  after?: number;
  /** Default 110 000 (D17), cap 600 000. */
  timeout_ms?: number;
};
export type WaitResult = {
  events: Event[];
  status: RunStatus;
  gate?: Gate;
  /** `true` when the run is `completed`, `failed` or `cancelled`. */
  done: boolean;
};

export type AnswerParams = { run_id: string; gate_id: string; value: string | string[] };
export type AnswerResult = { accepted: boolean };

export type StatusParams = { run_id?: string };
export type StatusResult = RunSummary | RunSummary[];

export type CancelParams = { run_id: string; reason?: string };
export type CancelResult = { status: "cancelled" };

export type ResumeParams = { run_id: string };
export type ResumeResult = { run_id: string; status: RunStatus };

export type ReportParams = { run_id: string };
export type ReportResult = {
  units: UnitRow[];
  /** Per pool, per harness and per step (M6.1). */
  usage: UsageByPool;
  /** Both pools folded into one figure. */
  usage_total: Usage;
  /** Harness / model / effort per step (`<step>.<phase>` keys for `units` steps). */
  resolved: Record<string, Resolved>;
  /** Step id → one-line verdict. */
  verdicts: Record<string, string>;
};

// ---- child channel (P8, D16) --------------------------------------------------------------
// Every `child_*` method carries the per-step `token` the daemon put in the child's MCP env;
// an unknown or ended token is the domain error `TOKEN_INVALID`.

export type ChildReportParams = {
  token: string;
  kind: ReportKind;
  text: string;
  data?: Record<string, unknown>;
};
export type ChildReportResult = { accepted: true; seq: number };

export type ChildAskParams = {
  token: string;
  question: string;
  options?: string[];
  allow_text?: boolean;
  /** Re-issue after a `pending` result to keep waiting on the same question. */
  ask_id?: string;
  /** Long-poll bound, default `WAIT_DEFAULT_MS`, cap `WAIT_MAX_MS`. */
  timeout_ms?: number;
};
export type ChildAskResult =
  | { ask_id: string; status: "answered"; value: string }
  | { ask_id: string; status: "pending" }
  /** Synchronous run with nothing to answer from: the child must finish without it. */
  | { ask_id: string; status: "needs-human" };

export type ChildContextParams = { token: string; key: string };
/** `null` when the key resolves to nothing. */
export type ChildContextResult = { value: unknown };

export type ChildCheckpointParams = { token: string; data: unknown };
export type ChildCheckpointResult = { path: string };

/** Harness-facing: a mid-run user message to a live Claude child. */
export type NudgeParams = { run_id: string; step: string; message: string };
export type NudgeResult = { delivered: boolean };

/** Server → client notification sent every 30 s while a `wait` is blocked (D17). */
export type ProgressParams = { run_id: string; waiting_ms: number };

/** Every method with its params and result, so client and server share one signature table. */
export type Methods = {
  hello: { params: HelloParams; result: HelloResult };
  shutdown: { params: ShutdownParams; result: ShutdownResult };
  preflight: { params: PreflightParams; result: PreflightResult };
  run: { params: RunParams; result: RunResult };
  wait: { params: WaitParams; result: WaitResult };
  answer: { params: AnswerParams; result: AnswerResult };
  status: { params: StatusParams; result: StatusResult };
  cancel: { params: CancelParams; result: CancelResult };
  resume: { params: ResumeParams; result: ResumeResult };
  report: { params: ReportParams; result: ReportResult };
  nudge: { params: NudgeParams; result: NudgeResult };
  child_report: { params: ChildReportParams; result: ChildReportResult };
  child_ask: { params: ChildAskParams; result: ChildAskResult };
  child_context: { params: ChildContextParams; result: ChildContextResult };
  child_checkpoint: { params: ChildCheckpointParams; result: ChildCheckpointResult };
};
export type MethodName = keyof Methods;
export type ParamsOf<M extends MethodName> = Methods[M]["params"];
export type ResultOf<M extends MethodName> = Methods[M]["result"];

export type Notifications = {
  progress: ProgressParams;
};
export type NotificationName = keyof Notifications;

export const METHOD_NAMES: readonly MethodName[] = [
  "hello",
  "shutdown",
  "preflight",
  "run",
  "wait",
  "answer",
  "status",
  "cancel",
  "resume",
  "report",
  "nudge",
  "child_report",
  "child_ask",
  "child_context",
  "child_checkpoint",
];

// ---- wait limits (D17) ------------------------------------------------------------------

export const WAIT_DEFAULT_MS = 110_000;
export const WAIT_MAX_MS = 600_000;
export const WAIT_PROGRESS_MS = 30_000;
export const WAIT_POLL_MS = 250;
