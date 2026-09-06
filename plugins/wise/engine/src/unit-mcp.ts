// `wise-engine unit-mcp`: the child-side stdio MCP server (research-ts-engine.md P8, D16). A
// harness child loads it through `--mcp-config`; it is a thin client of the daemon socket that
// presents the per-step token from its env on every call. It never starts the daemon: the daemon
// spawned the child, so a dead socket means the run is gone.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { exitAfterClose, watchHost } from "./host-watch.ts";
import { z } from "zod";
import type { ClientOptions } from "./client.ts";
import type { DaemonIo } from "./daemon.ts";
import { DaemonLink, errResult, okResult, registerTool, toErrorResult } from "./mcp.ts";
import type { Env } from "./paths.ts";
import { WAIT_DEFAULT_MS, WAIT_MAX_MS } from "./protocol.ts";
import type {
  ChildAskParams,
  ChildAskResult,
  ChildCheckpointParams,
  ChildContextParams,
  ChildReportParams,
} from "./protocol.ts";
import { REPORT_KINDS } from "./types.ts";
import { pluginVersion } from "./version.ts";

export const UNIT_MCP_SERVER_NAME = "wise-engine";
export const TOKEN_VAR = "WISE_STEP_TOKEN";
export const SOCKET_VAR = "WISE_ENGINE_SOCKET";
export const DATA_ROOT_VAR = "WISE_DATA_ROOT";

export const UNIT_MCP_TOOL_NAMES = [
  "wise_report",
  "wise_ask",
  "wise_context",
  "wise_checkpoint",
] as const;
export type UnitMcpToolName = (typeof UNIT_MCP_TOOL_NAMES)[number];

/** Headroom on the socket call over the daemon-side long-poll bound. */
const ASK_CALL_HEADROOM_MS = 30_000;

export type UnitMcpOptions = {
  /** Per-step token; default `$WISE_STEP_TOKEN`. */
  token?: string;
  /** Socket, data root and version for the daemon connection. */
  daemon?: ClientOptions;
  version?: string;
  /** Per-call long-poll bound for `wise_ask`; default `WAIT_DEFAULT_MS`, cap `WAIT_MAX_MS`. */
  askTimeoutMs?: number;
};

// ---- schemas -------------------------------------------------------------------------------------

const reportShape = {
  kind: z.enum(REPORT_KINDS).describe("progress | blocker | decision | finding"),
  text: z.string().describe("One line, <= 200 chars; longer text is clipped."),
  data: z
    .record(z.unknown())
    .optional()
    .describe("Small JSON payload, <= 1 kB; dropped when larger."),
};
const askShape = {
  question: z.string(),
  options: z.array(z.string()).optional().describe("Choices offered to the human."),
  allow_text: z.boolean().optional().describe("Accept free text; default true when no options."),
};
const contextShape = {
  key: z
    .string()
    .describe("ticket | guidance | decisions | links | <output name> | <step id> | <input name>"),
};
const checkpointShape = { data: z.unknown().describe("Partial result as JSON.") };

const DESCRIPTIONS: Record<UnitMcpToolName, string> = {
  wise_report:
    "Tell the orchestrator how you are doing. kind progress: milestone reached; blocker: something " +
    "stops you; decision: a choice you made; finding: a fact worth recording. Keep text to one line.",
  wise_ask:
    "Ask the human a question and wait for the answer. Blocks until answered; returns {value}. " +
    "Give options when a choice is enough. In an unattended run the answer comes from the run's " +
    "recorded decisions or the first option; {error: 'needs-human'} means proceed with your best judgement.",
  wise_context:
    "Fetch run context by key instead of guessing: ticket (array with body), guidance, decisions, " +
    "links, a prior step's output by name or step id, or an input. Returns {value}; null when unknown.",
  wise_checkpoint:
    "Save partial results as JSON so they survive if you are stopped. Overwrites the previous checkpoint.",
};

// ---- server -------------------------------------------------------------------------------------------

function clientOptsFromEnv(env: Env): ClientOptions {
  const opts: ClientOptions = { env, client: "wise-engine unit-mcp" };
  if (env[SOCKET_VAR]) opts.socketPath = env[SOCKET_VAR];
  if (env[DATA_ROOT_VAR]) opts.dataRoot = env[DATA_ROOT_VAR];
  return opts;
}

/** Build the child-side server; connect it to a transport yourself. */
export function createUnitMcpServer(opts: UnitMcpOptions = {}): McpServer {
  const env = opts.daemon?.env ?? process.env;
  const token = opts.token ?? env[TOKEN_VAR] ?? "";
  const link = new DaemonLink({
    daemon: opts.daemon ?? clientOptsFromEnv(env),
    autoStart: false,
  });
  const server = new McpServer({
    name: UNIT_MCP_SERVER_NAME,
    version: opts.version ?? opts.daemon?.version ?? pluginVersion(),
  });
  // oxlint-disable-next-line unicorn/prefer-add-event-listener
  server.server.onclose = () => link.close();
  const askTimeoutMs = Math.min(Math.max(0, opts.askTimeoutMs ?? WAIT_DEFAULT_MS), WAIT_MAX_MS);

  const noToken = (): ReturnType<typeof errResult> | undefined =>
    token === "" ? errResult("TOKEN_INVALID", `${TOKEN_VAR} is not set for this child`) : undefined;

  registerTool(server, "wise_report", DESCRIPTIONS.wise_report, reportShape, async (args) => {
    const missing = noToken();
    if (missing) return missing;
    const params: ChildReportParams = { token, kind: args.kind, text: args.text };
    if (args.data !== undefined) params.data = args.data;
    try {
      return okResult(await link.withClient((c) => c.call("child_report", params)));
    } catch (err) {
      return toErrorResult(err);
    }
  });

  registerTool(server, "wise_ask", DESCRIPTIONS.wise_ask, askShape, async (args) => {
    const missing = noToken();
    if (missing) return missing;
    const params: ChildAskParams = { token, question: args.question, timeout_ms: askTimeoutMs };
    if (args.options !== undefined) params.options = args.options;
    if (args.allow_text !== undefined) params.allow_text = args.allow_text;
    try {
      // Long-poll: a `pending` result means the bound passed; re-issue with ask_id until answered.
      for (;;) {
        const res: ChildAskResult = await link.withClient((c) =>
          c.call("child_ask", params, { timeoutMs: askTimeoutMs + ASK_CALL_HEADROOM_MS }),
        );
        if (res.status === "answered") return okResult({ value: res.value });
        if (res.status === "needs-human") {
          return errResult(
            "needs-human",
            "no human is attached and no decision covers this question",
            {
              error: "needs-human",
            },
          );
        }
        params.ask_id = res.ask_id;
      }
    } catch (err) {
      return toErrorResult(err);
    }
  });

  registerTool(server, "wise_context", DESCRIPTIONS.wise_context, contextShape, async (args) => {
    const missing = noToken();
    if (missing) return missing;
    const params: ChildContextParams = { token, key: args.key };
    try {
      return okResult(await link.withClient((c) => c.call("child_context", params)));
    } catch (err) {
      return toErrorResult(err);
    }
  });

  registerTool(
    server,
    "wise_checkpoint",
    DESCRIPTIONS.wise_checkpoint,
    checkpointShape,
    async (args) => {
      const missing = noToken();
      if (missing) return missing;
      const params: ChildCheckpointParams = { token, data: args.data ?? null };
      try {
        return okResult(await link.withClient((c) => c.call("child_checkpoint", params)));
      } catch (err) {
        return toErrorResult(err);
      }
    },
  );

  return server;
}

/** Serve on stdin/stdout until the child closes the pipe. Resolves with the exit code. */
export async function serveUnitStdio(opts: UnitMcpOptions = {}): Promise<number> {
  const server = createUnitMcpServer(opts);
  const { promise, resolve } = Promise.withResolvers<number>();
  const onclose = server.server.onclose;
  // oxlint-disable-next-line unicorn/prefer-add-event-listener
  server.server.onclose = () => {
    onclose?.();
    stop();
    resolve(0);
  };
  const transport = new StdioServerTransport();
  // The SDK transport never sees stdin EOF or a dead parent; without this the server outlives
  // its child as an orphan and Bun spins on the closed pipe.
  const stop = watchHost({
    stdin: process.stdin,
    stdout: process.stdout,
    ppid: () => process.ppid,
    onGone: () => {
      transport.close().catch(() => {});
    },
  });
  await server.connect(transport);
  const code = await promise;
  exitAfterClose(code);
  return code;
}

// ---- CLI: `wise-engine unit-mcp [options]` ----------------------------------------------------------------

const UNIT_MCP_USAGE = `wise-engine unit-mcp [options]

  Child-side stdio MCP server: wise_report, wise_ask, wise_context, wise_checkpoint.
  Reads ${TOKEN_VAR}, ${SOCKET_VAR}, ${DATA_ROOT_VAR} from the env; never starts the daemon.

Options: --token <t> --socket <path> --data-root <dir>
`;

function parseFlags(argv: readonly string[]): Record<string, string | true> {
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

/** Entry for the CLI owner: `case "unit-mcp": return unitMcpCommand(argv.slice(1), io)`. */
export async function unitMcpCommand(argv: string[], io: DaemonIo): Promise<number> {
  const flags = parseFlags(argv);
  if (flags.help === true || argv[0] === "-h") {
    io.err(UNIT_MCP_USAGE);
    return 0;
  }
  const env = io.env ?? process.env;
  const s = (k: string): string | undefined =>
    typeof flags[k] === "string" ? (flags[k] as string) : undefined;
  const daemon = clientOptsFromEnv(env);
  const socket = s("socket");
  const dataRoot = s("data-root");
  if (socket) daemon.socketPath = socket;
  if (dataRoot) daemon.dataRoot = dataRoot;
  const opts: UnitMcpOptions = { daemon };
  const token = s("token");
  if (token) opts.token = token;
  try {
    return await serveUnitStdio(opts);
  } catch (err) {
    io.err(`wise-engine unit-mcp: ${(err as Error).message}\n`);
    return 70;
  }
}
