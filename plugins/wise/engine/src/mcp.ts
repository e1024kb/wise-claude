// `wise-engine mcp`: a stdio MCP server that is a thin client of wise-engined (D13, P1).
// The eight tools mirror the daemon's P1 methods with identical JSON shapes. The server connects to
// the socket lazily, auto-starts the daemon when it is dead, reconnects once on a dropped socket,
// and turns daemon errors into tool error results instead of protocol failures. During `wise_wait`
// the daemon's `progress` notifications are forwarded as MCP progress notifications (D17).

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import type { RequestHandlerExtra } from "@modelcontextprotocol/sdk/shared/protocol.js";
import type {
  CallToolResult,
  ServerNotification,
  ServerRequest,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { connect, ConnectError, ensureDaemon } from "./client.ts";
import type { Client, ClientOptions } from "./client.ts";
import type { DaemonIo } from "./daemon.ts";
import type { Env } from "./paths.ts";
import { RPC_CLIENT_DISCONNECTED, WAIT_DEFAULT_MS, WAIT_MAX_MS } from "./protocol.ts";
import type {
  AnswerParams,
  CancelParams,
  MethodName,
  NudgeParams,
  ParamsOf,
  PreflightParams,
  ProgressParams,
  ResultOf,
  ResumeParams,
  RunParams,
  StatusParams,
  WaitParams,
} from "./protocol.ts";
import { domainCode, RpcError } from "./rpc.ts";
import type { CallOptions } from "./rpc.ts";
import type { Context } from "./types.ts";
import { pluginVersion } from "./version.ts";

// ---- options -------------------------------------------------------------------------------

export type McpServerOptions = {
  /** Socket, data root, version and start options for the daemon connection. */
  daemon?: ClientOptions;
  /** Version reported in the MCP `serverInfo`; defaults to the plugin version. */
  version?: string;
  /** Spawn the daemon when the socket is dead. Default true. */
  autoStart?: boolean;
};

export const MCP_SERVER_NAME = "wise-engine";
/** Headroom added to the socket call timeout on top of the requested `wait` timeout. */
const WAIT_CALL_HEADROOM_MS = 30_000;
const INIT_HINT = "wise-engined is not reachable; run /wise-init, then retry.";

export const MCP_TOOL_NAMES = [
  "wise_preflight",
  "wise_run",
  "wise_wait",
  "wise_answer",
  "wise_status",
  "wise_cancel",
  "wise_nudge",
  "wise_resume",
] as const;
export type McpToolName = (typeof MCP_TOOL_NAMES)[number];

// ---- daemon link ------------------------------------------------------------------------------

/** One lazily opened socket shared by every tool call, replaced when it drops. */
export class DaemonLink {
  private client: Client | null = null;
  private opening: Promise<Client> | null = null;
  private readonly opts: McpServerOptions;
  constructor(opts: McpServerOptions) {
    this.opts = opts;
  }

  private open(): Promise<Client> {
    if (this.opening) return this.opening;
    const daemon = this.opts.daemon ?? {};
    const p = (this.opts.autoStart === false ? connect(daemon) : ensureDaemon(daemon))
      .then((c) => {
        this.client = c;
        return c;
      })
      .finally(() => {
        this.opening = null;
      });
    this.opening = p;
    return p;
  }

  private async current(): Promise<Client> {
    if (this.client) return this.client;
    return this.open();
  }

  private drop(c: Client): void {
    if (this.client === c) this.client = null;
    c.close();
  }

  /** Run `fn` against the live client; a closed socket triggers one reconnect and retry. */
  async withClient<T>(fn: (client: Client) => Promise<T>): Promise<T> {
    const first = await this.current();
    try {
      return await fn(first);
    } catch (err) {
      if (!(err instanceof RpcError) || err.code !== RPC_CLIENT_DISCONNECTED) throw err;
      this.drop(first);
    }
    const second = await this.open();
    return fn(second);
  }

  close(): void {
    if (this.client) this.drop(this.client);
  }
}

// ---- results -------------------------------------------------------------------------------------

type ToolExtra = RequestHandlerExtra<ServerRequest, ServerNotification>;

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Compact JSON in one text block; `structuredContent` only for object results (MCP requires an object). */
export function okResult(result: unknown): CallToolResult {
  const out: CallToolResult = { content: [{ type: "text", text: JSON.stringify(result) }] };
  if (isPlainObject(result)) out.structuredContent = result;
  return out;
}

export function errResult(
  code: string,
  message: string,
  extra: Record<string, unknown> = {},
): CallToolResult {
  const error = { code, message, ...extra };
  return { content: [{ type: "text", text: JSON.stringify({ error }) }], isError: true };
}

/** Daemon and socket failures become tool error results; anything else stays a protocol error. */
export function toErrorResult(err: unknown): CallToolResult {
  if (err instanceof ConnectError) {
    return errResult("DAEMON_UNAVAILABLE", err.message, { cause: err.code, hint: INIT_HINT });
  }
  if (err instanceof RpcError) {
    const code = domainCode(err);
    const data = isPlainObject(err.data) ? err.data : {};
    const rest = Object.fromEntries(Object.entries(data).filter(([k]) => k !== "code"));
    if (code !== undefined) return errResult(code, err.message, rest);
    return errResult("RPC_ERROR", err.message, { rpc_code: err.code, ...rest });
  }
  throw err;
}

// ---- schemas ---------------------------------------------------------------------------------------

const answerValue = z.union([z.string(), z.array(z.string())]);
const answersSchema = z.record(answerValue).describe("Question id -> value, from wise_preflight.");
const contextSchema = z
  .object({
    ticket: z
      .array(
        z.object({
          ref: z.string(),
          title: z.string().optional(),
          body: z.string().optional(),
          url: z.string().optional(),
        }),
      )
      .optional()
      .describe("Tickets already fetched in this conversation, with their bodies."),
    guidance: z.string().optional().describe("Operator free-text guidance for the run."),
    decisions: z
      .record(z.string())
      .optional()
      .describe("Decisions already settled in the conversation."),
    links: z.array(z.string()).optional(),
  })
  .describe(
    "What the conversation already knows and the run needs. Children never see this transcript.",
  );

const preflightShape = {
  workflow: z.string().describe("Workflow name or path to its YAML."),
  cwd: z.string().describe("Absolute path of the target project."),
  answers: answersSchema
    .optional()
    .describe(
      "Answers collected so far (question id -> value). Pass them back to get the next stage: " +
        "harness.<group> unlocks model.<group>, which unlocks effort.<group>.",
    ),
};
const runShape = {
  workflow: z.string().describe("Workflow name or path to its YAML."),
  cwd: z.string().describe("Absolute path of the target project."),
  answers: answersSchema.default({}),
  context: contextSchema.default({}),
  inputs: z.record(z.string()).default({}).describe("Workflow inputs by name."),
};
const resumeShape = { run_id: z.string() };
const waitShape = {
  run_id: z.string(),
  after: z
    .number()
    .int()
    .min(0)
    .optional()
    .describe("Return events with seq greater than this; the last seq you saw. Default 0."),
  timeout_ms: z
    .number()
    .int()
    .min(0)
    .optional()
    .describe(`Default ${WAIT_DEFAULT_MS}, capped at ${WAIT_MAX_MS}.`),
};
const answerShape = {
  run_id: z.string(),
  gate_id: z.string(),
  value: answerValue.describe("Chosen option value, free text when allowed, or a list for multi."),
};
const statusShape = { run_id: z.string().optional() };
const cancelShape = { run_id: z.string(), reason: z.string().optional() };
const nudgeShape = {
  run_id: z.string(),
  step: z.string().describe("Step id of the running agent child."),
  message: z.string().describe("Text delivered to the child as a user message."),
};

// ---- descriptions (read by the model that calls the tools) --------------------------------------------

const DESCRIPTIONS: Record<McpToolName, string> = {
  wise_preflight:
    "Call before wise_run, in a loop. Returns the questions the answers so far leave open " +
    "{workflow, version, questions, defaults}: ask them, then call again with every answer collected " +
    "until questions is empty, then wise_run. Per tuning group the stages are harness.<group> (which " +
    "logged-in CLI: claude, codex, grok, gemini; only asked when two or more are ready), model.<group> " +
    "(that harness's model catalog) and effort.<group> (that model's efforts; skipped when it has one " +
    "or none). step-select and input.<name> come with the first call. requires_missing lists " +
    "plugin:<name> / tool:<name> the workflow declares but the machine lacks; wise_run refuses with " +
    "REQUIRES_MISSING until they are installed. Read-only, starts nothing.",
  wise_run:
    "Start a workflow run; returns {run_id, status} at once, then loop on wise_wait. " +
    "answers: question id -> value from wise_preflight. context: build it from what this conversation " +
    "already knows (ticket refs with title and body, operator guidance, decisions made, links); child " +
    "agents never see the transcript, so anything missing here they must refetch. inputs: workflow inputs by name. " +
    "AUTH_REQUIRED errors carry login_cmd: show it to the user verbatim. MISSING_ANSWERS lists required " +
    "questions or inputs left without a value (ask the user, then retry).",
  wise_wait:
    "Long-poll a run. Blocks until a new event past `after`, an open gate, the run ending, or timeout_ms " +
    `(default ${WAIT_DEFAULT_MS}, max ${WAIT_MAX_MS}), then returns {events, status, gate?, done}. ` +
    "Events are one-line verdicts plus structured outputs; step logs and agent output never appear. " +
    "gate present: ask the user, call wise_answer. done true: stop. Otherwise call again with after = last event seq. " +
    "Progress notifications are sent every 30 s while blocked.",
  wise_answer:
    "Answer the gate wise_wait returned. value: the option value, free text when allow_text, or a string " +
    "array for multi. Returns {accepted}. GATE_STALE means the gate is already closed: call wise_wait again.",
  wise_status:
    "Run summaries, no step output: one RunSummary for run_id, or every known run without it, newest " +
    "activity first. Cheapest daemon health check; DAEMON_UNAVAILABLE means /wise-init has not run.",
  wise_cancel:
    "Cancel a run and kill its child processes. Returns {status: 'cancelled'}. reason is recorded in the run's events.",
  wise_nudge:
    "Send a mid-run user message to a running agent step (Claude children only; their stdin stays open). " +
    "Returns {delivered}. delivered false: the step is not running, has ended, or its harness takes no input; " +
    "nothing is queued. Use sparingly: to unblock, redirect, or ask a child to wrap up.",
  wise_resume:
    "Resume a paused or failed run: in-flight steps reset to pending and the scheduler continues from the ledger. " +
    "Returns {run_id, status}; then loop on wise_wait. A gated run is not resumable: answer its gate with wise_answer. " +
    "Completed and cancelled runs are refused.",
};

// ---- server ---------------------------------------------------------------------------------------------

function progressNotification(
  token: string | number,
  p: ProgressParams,
): ServerNotification & { method: "notifications/progress" } {
  const seconds = Math.round(p.waiting_ms / 1000);
  return {
    method: "notifications/progress",
    params: {
      progressToken: token,
      progress: p.waiting_ms,
      message: `wise_wait: run ${p.run_id} has been waiting for ${seconds} s`,
    },
  };
}

function isProgress(params: unknown): params is ProgressParams {
  return (
    isPlainObject(params) &&
    typeof params.run_id === "string" &&
    typeof params.waiting_ms === "number"
  );
}

export type Shape = Record<string, z.ZodTypeAny>;
export type ToolHandler<S extends Shape> = (
  args: z.infer<z.ZodObject<S>>,
  extra: ToolExtra,
) => Promise<CallToolResult>;

/** `registerTool` seen through a loose signature: the SDK's zod-compat inference is too deep for tsgo. */
type LooseRegisterTool = {
  registerTool: (
    name: string,
    config: { description: string; inputSchema: Shape },
    handler: (args: never, extra: ToolExtra) => Promise<CallToolResult>,
  ) => unknown;
};

/** Register one tool with the handler typed from our own zod shape (shared with unit-mcp.ts). */
export function registerTool<S extends Shape>(
  server: McpServer,
  name: string,
  description: string,
  shape: S,
  handler: ToolHandler<S>,
): void {
  (server as unknown as LooseRegisterTool).registerTool(
    name,
    { description, inputSchema: shape },
    handler,
  );
}

function register<S extends Shape>(
  server: McpServer,
  name: McpToolName,
  shape: S,
  handler: ToolHandler<S>,
): void {
  registerTool(server, name, DESCRIPTIONS[name], shape, handler);
}

/** Build the MCP server with the eight harness-facing tools; connect it to a transport yourself. */
export function createMcpServer(opts: McpServerOptions = {}): McpServer {
  const link = new DaemonLink(opts);
  const server = new McpServer({
    name: MCP_SERVER_NAME,
    version: opts.version ?? opts.daemon?.version ?? pluginVersion(),
  });
  // Drop the socket when the host closes the transport (`onclose` is the SDK's hook, not a DOM event).
  // oxlint-disable-next-line unicorn/prefer-add-event-listener
  server.server.onclose = () => link.close();

  const forward = async <M extends MethodName>(
    method: M,
    params: ParamsOf<M>,
    callOpts?: CallOptions,
    onClient?: (client: Client) => () => void,
  ): Promise<CallToolResult> => {
    try {
      const result = await link.withClient(async (client) => {
        const off = onClient?.(client);
        try {
          return (await client.call(method, params, callOpts)) as ResultOf<M>;
        } finally {
          off?.();
        }
      });
      return okResult(result);
    } catch (err) {
      return toErrorResult(err);
    }
  };

  register(server, "wise_preflight", preflightShape, async (args) => {
    const params: PreflightParams = { workflow: args.workflow, cwd: args.cwd };
    if (args.answers !== undefined) params.answers = args.answers;
    return forward("preflight", params);
  });

  register(server, "wise_run", runShape, async (args) => {
    const params: RunParams = {
      workflow: args.workflow,
      cwd: args.cwd,
      answers: args.answers,
      context: args.context as Context,
      inputs: args.inputs,
    };
    return forward("run", params);
  });

  register(server, "wise_wait", waitShape, async (args, extra) => {
    const timeoutMs = Math.min(Math.max(0, args.timeout_ms ?? WAIT_DEFAULT_MS), WAIT_MAX_MS);
    const params: WaitParams = { run_id: args.run_id, timeout_ms: timeoutMs };
    if (args.after !== undefined) params.after = args.after;
    // oxlint-disable-next-line no-underscore-dangle
    const token = extra._meta?.progressToken;
    const subscribe =
      token === undefined
        ? undefined
        : (client: Client) =>
            client.notifications.on((n) => {
              if (n.method !== "progress" || !isProgress(n.params)) return;
              if (n.params.run_id !== args.run_id) return;
              void extra.sendNotification(progressNotification(token, n.params)).catch(() => {
                // The host went away mid-wait; the call result will fail on its own.
              });
            });
    return forward("wait", params, { timeoutMs: timeoutMs + WAIT_CALL_HEADROOM_MS }, subscribe);
  });

  register(server, "wise_answer", answerShape, async (args) => {
    const params: AnswerParams = { run_id: args.run_id, gate_id: args.gate_id, value: args.value };
    return forward("answer", params);
  });

  register(server, "wise_status", statusShape, async (args) => {
    const params: StatusParams = {};
    if (args.run_id !== undefined) params.run_id = args.run_id;
    return forward("status", params);
  });

  register(server, "wise_cancel", cancelShape, async (args) => {
    const params: CancelParams = { run_id: args.run_id };
    if (args.reason !== undefined) params.reason = args.reason;
    return forward("cancel", params);
  });

  register(server, "wise_nudge", nudgeShape, async (args) => {
    const params: NudgeParams = { run_id: args.run_id, step: args.step, message: args.message };
    return forward("nudge", params);
  });

  register(server, "wise_resume", resumeShape, async (args) => {
    const params: ResumeParams = { run_id: args.run_id };
    return forward("resume", params);
  });

  return server;
}

/** Serve on stdin/stdout until the host closes the pipe. Resolves with the exit code. */
export async function serveStdio(opts: McpServerOptions = {}): Promise<number> {
  const server = createMcpServer(opts);
  const { promise, resolve } = Promise.withResolvers<number>();
  const onclose = server.server.onclose;
  // oxlint-disable-next-line unicorn/prefer-add-event-listener
  server.server.onclose = () => {
    onclose?.();
    resolve(0);
  };
  await server.connect(new StdioServerTransport());
  return promise;
}

// ---- CLI: `wise-engine mcp [options]` --------------------------------------------------------------------

const MCP_USAGE = `wise-engine mcp [options]

  Serve the eight wise_* tools over stdio MCP; connects to (and starts) wise-engined.

Options: --data-root <dir> --socket <path> --lock <path> --log <path> --idle-ms <n> --no-start
`;

function parseMcpArgs(argv: readonly string[]): Record<string, string | true> {
  const flags: Record<string, string | true> = {};
  for (let i = 0; i < argv.length; i++) {
    const tok = argv[i] as string;
    if (!tok.startsWith("--")) continue;
    const eq = tok.indexOf("=");
    if (eq > 0) {
      flags[tok.slice(2, eq)] = tok.slice(eq + 1);
      continue;
    }
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("--")) {
      flags[tok.slice(2)] = next;
      i++;
    } else {
      flags[tok.slice(2)] = true;
    }
  }
  return flags;
}

function clientOptsFrom(flags: Record<string, string | true>, env: Env): ClientOptions {
  const opts: ClientOptions = { env, client: "wise-engine mcp" };
  const s = (k: string): string | undefined =>
    typeof flags[k] === "string" ? (flags[k] as string) : undefined;
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

/** Entry for the CLI owner: `case "mcp": return mcpCommand(argv.slice(1), io)`. stdout is the MCP pipe. */
export async function mcpCommand(argv: string[], io: DaemonIo): Promise<number> {
  const flags = parseMcpArgs(argv);
  if (flags.help === true || argv[0] === "-h") {
    io.err(MCP_USAGE);
    return 0;
  }
  const env = io.env ?? process.env;
  const opts: McpServerOptions = { daemon: clientOptsFrom(flags, env) };
  if (flags["no-start"] === true) opts.autoStart = false;
  try {
    return await serveStdio(opts);
  } catch (err) {
    io.err(`wise-engine mcp: ${(err as Error).message}\n`);
    return 70;
  }
}

if (import.meta.main ?? process.argv[1] === new URL(import.meta.url).pathname) {
  mcpCommand(process.argv.slice(2), {
    out: (s) => process.stdout.write(s),
    err: (s) => process.stderr.write(s),
  }).then((code) => {
    process.exitCode = code;
  });
}
