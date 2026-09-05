// `agent` step: build the P6 RunReq from the rendered step and its resolution, start the harness
// child, stream the vendor events into the raw log, and turn the RunRes into step outputs.

import { appendRawLog, writeLog } from "../ledger.ts";
import { effortFor } from "../resolve.ts";
import type {
  AgentStep,
  Effort,
  ExitClass,
  Harness,
  RawEvent,
  Resolved,
  RunReq,
  RunRes,
  Usage,
} from "../types.ts";

export const DEFAULT_STEP_TIMEOUT_MS = 30 * 60 * 1000;
export const VERDICT_MAX = 200;
const LOG_HEAD_BYTES = 2048;

/** First non-empty line of `text`, whitespace collapsed, clipped to `max` chars (E1 verdicts). */
export function headline(text: string, max: number = VERDICT_MAX): string {
  const line = text
    .split("\n")
    .map((l) => l.trim())
    .find((l) => l.length > 0);
  const flat = (line ?? "").replace(/\s+/g, " ");
  return flat.length > max ? flat.slice(0, max - 1) + "…" : flat;
}

/** A started harness child. `startClaude` returns a superset; other adapters give `done` only. */
export type AgentHandle = {
  pid?: number;
  done: Promise<RunRes>;
  /** Mid-run user message (Claude only, M2.6). */
  nudge?: (text: string) => void;
  kill?: (signal?: NodeJS.Signals) => void;
};

export type AgentStarter = (
  harness: Harness,
  req: RunReq,
  onEvent: (e: RawEvent) => void,
) => AgentHandle;

/** Where the child's `wise-engine unit-mcp` finds the daemon (P8): no secrets in argv, all in env. */
export type ChannelConfig = { engineRoot: string; socketPath: string; dataRoot: string };

/** The one MCP server every child loads (D16, D18). `bash engine.sh unit-mcp` picks bun or node. */
export function childMcpConfig(
  channel: ChannelConfig,
  token: string,
): NonNullable<RunReq["mcp_config"]> {
  return {
    mcpServers: {
      "wise-engine": {
        command: "bash",
        args: [`${channel.engineRoot}/engine.sh`, "unit-mcp"],
        env: {
          WISE_STEP_TOKEN: token,
          WISE_ENGINE_SOCKET: channel.socketPath,
          WISE_DATA_ROOT: channel.dataRoot,
        },
      },
    },
  };
}

export type AgentStepInput = {
  runDir: string;
  stepRunId: string;
  /** Already rendered (`renderStep`). */
  step: AgentStep;
  resolved: Resolved;
  cwd: string;
  /** Stored cursor from the previous attempt; honoured only under `resume: unit`. */
  cursor?: unknown;
  stepToken: string;
  starter: AgentStarter;
  defaultTimeoutMs?: number;
  /** When set, the child gets the engine MCP server with this token in its env. */
  channel?: ChannelConfig;
  /** Live-status hook: every vendor event, before it is logged. */
  onEvent?: (e: RawEvent) => void;
};

export type AgentOutcome = {
  exit: ExitClass | "missing_output";
  ok: boolean;
  outputs: Record<string, unknown>;
  verdict: string;
  error?: string;
  usage: Usage;
  cursor?: unknown;
  warnings: string[];
};

export function buildRunReq(input: AgentStepInput): RunReq {
  const { step, resolved } = input;
  const req: RunReq = {
    prompt: step.prompt,
    model: resolved.model,
    cwd: input.cwd,
    mode: step.mode ?? "auto",
    timeout_ms:
      step.timeout !== undefined
        ? step.timeout * 1000
        : (input.defaultTimeoutMs ?? DEFAULT_STEP_TIMEOUT_MS),
    auth: step.auth ?? "subscription",
    step_token: input.stepToken,
  };
  if (resolved.effort !== "" && effortFor(resolved.harness, resolved.effort) !== undefined) {
    req.effort = resolved.effort as Effort;
  }
  if (step.schema !== undefined) req.schema = step.schema;
  if (step.max_turns !== undefined) req.max_turns = step.max_turns;
  // E8: only the `unit` policy resumes a prior session; `fresh` (default) starts clean.
  if (step.resume === "unit" && input.cursor !== undefined) req.resume = input.cursor;
  if (input.channel !== undefined) req.mcp_config = childMcpConfig(input.channel, input.stepToken);
  return req;
}

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);

/** Tool names in a vendor event, for the human log (Claude `assistant` shape; others add later). */
function toolNames(parsed: unknown): string[] {
  if (!isRec(parsed) || parsed.type !== "assistant" || !isRec(parsed.message)) return [];
  const content = parsed.message.content;
  if (!Array.isArray(content)) return [];
  return content
    .filter((b): b is Rec => isRec(b) && b.type === "tool_use")
    .map((b) => (typeof b.name === "string" ? b.name : "?"));
}

function excerpt(text: string): string {
  if (text.length <= 2 * LOG_HEAD_BYTES) return text;
  return `${text.slice(0, LOG_HEAD_BYTES)}\n\n[... ${text.length - 2 * LOG_HEAD_BYTES} chars elided ...]\n\n${text.slice(-LOG_HEAD_BYTES)}`;
}

export function humanLog(input: AgentStepInput, req: RunReq, res: RunRes, tools: string[]): string {
  const u = res.usage;
  const lines = [
    `step: ${input.step.id} (${input.stepRunId})`,
    `harness: ${input.resolved.harness} model: ${req.model} effort: ${req.effort ?? "-"} mode: ${req.mode}`,
    `exit: ${res.exit}${res.error ? ` error: ${res.error}` : ""}`,
    `usage: in=${u.input} out=${u.output} cache_read=${u.cache_read} cache_write=${u.cache_write}${u.cost_usd !== undefined ? ` cost_usd=${u.cost_usd}` : ""} pool=${u.pool}`,
    `tools: ${tools.length ? tools.join(", ") : "-"}`,
  ];
  if (res.cursor !== undefined) lines.push(`cursor: ${JSON.stringify(res.cursor)}`);
  if (res.warnings?.length) lines.push(`warnings: ${res.warnings.join("; ")}`);
  lines.push("", "--- text ---", excerpt(res.text));
  if (res.json !== undefined) lines.push("", "--- json ---", excerpt(JSON.stringify(res.json)));
  return lines.join("\n") + "\n";
}

/** Copy the declared output names out of the schema result; a missing name fails the step. */
export function extractOutputs(
  names: readonly string[] | undefined,
  json: unknown,
): { outputs: Record<string, unknown>; missing?: string } {
  const outputs: Record<string, unknown> = {};
  if (!names || names.length === 0) return { outputs };
  if (!isRec(json)) return { outputs, missing: names[0] as string };
  for (const name of names) {
    if (!(name in json) || json[name] === undefined) return { outputs, missing: name };
    outputs[name] = json[name];
  }
  return { outputs };
}

export function outcomeOf(step: AgentStep, res: RunRes): AgentOutcome {
  const base = { usage: res.usage, warnings: res.warnings ?? [] };
  const withCursor = (o: AgentOutcome): AgentOutcome => {
    if (res.cursor !== undefined) o.cursor = res.cursor;
    return o;
  };
  if (res.exit !== "ok") {
    const error = res.error ?? res.exit;
    return withCursor({
      ...base,
      exit: res.exit,
      ok: false,
      outputs: {},
      verdict: headline(`${res.exit}: ${error}`),
      error,
    });
  }
  const { outputs, missing } = extractOutputs(step.outputs, res.json);
  if (missing !== undefined) {
    const error = `schema result lacks ${missing}`;
    return withCursor({
      ...base,
      exit: "missing_output",
      ok: false,
      outputs: {},
      verdict: headline(error),
      error,
    });
  }
  const verdict =
    headline(res.text) || (res.json !== undefined ? headline(JSON.stringify(res.json)) : "ok");
  return withCursor({ ...base, exit: "ok", ok: true, outputs, verdict });
}

/** Start the child; `handle` is live at once (kill / nudge), `outcome` settles when it exits. */
export function startAgentStep(input: AgentStepInput): {
  handle: AgentHandle;
  req: RunReq;
  outcome: Promise<AgentOutcome>;
} {
  const req = buildRunReq(input);
  const tools: string[] = [];
  const onEvent = (e: RawEvent): void => {
    tools.push(...toolNames(e.parsed));
    try {
      input.onEvent?.(e);
    } catch {
      // A status hook failure never fails the step either.
    }
    try {
      appendRawLog(input.runDir, input.step.id, input.stepRunId, e);
    } catch {
      // A log write failure never fails the step.
    }
  };
  const handle = input.starter(input.resolved.harness, req, onEvent);
  const outcome = handle.done.then((res) => {
    try {
      writeLog(input.runDir, input.step.id, input.stepRunId, humanLog(input, req, res, tools));
    } catch {
      // Same: the ledger's step record is the source of truth.
    }
    return outcomeOf(input.step, res);
  });
  return { handle, req, outcome };
}
