// `claude` harness adapter (research-ts-engine.md P6, D3, D16, D18; M0 event shapes).
// Runs `claude -p --input-format stream-json --output-format stream-json` on the
// unmodified binary, never `--bare`. The prompt and any mid-run nudge go over stdin
// as NDJSON user messages; stdout NDJSON is parsed by a pure, testable stream parser.

import { execFile } from "node:child_process";
import type {
  AuthMode,
  Adapter,
  Effort,
  RawEvent,
  RunMode,
  RunReq,
  RunRes,
  Usage,
} from "../types.ts";
import { cleanEnv, createLineSplitter, spawnClean } from "./spawn.ts";
import type { SpawnExit } from "./spawn.ts";

export const CLAUDE_BIN = "claude";
export const CLAUDE_KEY_VAR = "ANTHROPIC_API_KEY";
export const CLAUDE_CONFIG_VAR = "CLAUDE_CONFIG_DIR";

/** T2: wise run mode to Claude `--permission-mode`. */
export const MODE_MAP: Record<RunMode, string> = {
  "approval-required": "default",
  auto: "acceptEdits",
  "full-access": "bypassPermissions",
};

/** D18: children never load the user's MCP servers. */
export const EMPTY_MCP_CONFIG = { mcpServers: {} } as const;

export const RATE_LIMIT_RE = /rate.?limit|429|overloaded/i;
export const AUTH_RE = /Failed to authenticate|OAuth|not logged in|login/i;
const ERROR_TEXT_MAX = 500;

// ---- argv and stdin ---------------------------------------------------------------------------

export function buildArgv(req: RunReq): string[] {
  const argv = [
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
  ];
  // `inherit` (or an empty pin) leaves the model to the child's own default.
  if (req.model && req.model !== "inherit") argv.push("--model", req.model);
  const effort = req.effort === undefined ? undefined : effortMap(req.effort);
  if (effort !== undefined) argv.push("--effort", effort);
  if (req.schema !== undefined) argv.push("--json-schema", JSON.stringify(req.schema));
  if (req.max_turns !== undefined) argv.push("--max-turns", String(req.max_turns));
  if (typeof req.resume === "string" && req.resume.length > 0) argv.push("--resume", req.resume);
  argv.push("--permission-mode", MODE_MAP[req.mode]);
  for (const dir of req.add_dirs ?? []) argv.push("--add-dir", dir);
  // Headless children cannot answer permission prompts: pre-grant the engine's own MCP server
  // and whatever the step declared.
  const allowed = [
    ...Object.keys(req.mcp_config?.mcpServers ?? {}).map((name) => `mcp__${name}`),
    ...(req.allowed_tools ?? []),
  ];
  if (allowed.length > 0) argv.push("--allowedTools", allowed.join(","));
  argv.push(
    "--strict-mcp-config",
    "--mcp-config",
    JSON.stringify(req.mcp_config ?? EMPTY_MCP_CONFIG),
  );
  if (req.system !== undefined) argv.push("--append-system-prompt", req.system);
  return argv;
}

/** One `--input-format stream-json` user message, newline-terminated. */
export function userMessage(text: string): string {
  return `${JSON.stringify({
    type: "user",
    message: { role: "user", content: [{ type: "text", text }] },
  })}\n`;
}

export function childEnv(
  req: Pick<RunReq, "auth" | "env">,
  parent?: NodeJS.ProcessEnv,
): Record<string, string> {
  return cleanEnv({
    ...(parent ? { parent } : {}),
    keep: [CLAUDE_CONFIG_VAR],
    secrets: req.auth === "api-key" ? [CLAUDE_KEY_VAR] : [],
    ...(req.env ? { extra: req.env } : {}),
  });
}

export function effortMap(e: Effort): string | undefined {
  return e;
}

// ---- stream parser --------------------------------------------------------------------------------

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const num = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;

/** Diagnostics collected from the stream, exposed for progress events. */
export type StreamSnapshot = {
  session_id?: string;
  model?: string;
  tools: number;
  turns: number;
  tool_uses: string[];
  results: number;
  denials: string[];
};

export type StreamParser = {
  /** Feed a stdout chunk; returns one RawEvent per completed line. */
  feed(chunk: string): RawEvent[];
  /** Flush the trailing partial line, then classify the run. */
  finish(exit: SpawnExit): RunRes;
  snapshot(): StreamSnapshot;
};

function assistantText(message: unknown): string {
  if (!isRec(message) || !Array.isArray(message.content)) return "";
  return message.content
    .filter((b): b is Rec => isRec(b) && b.type === "text")
    .map((b) => str(b.text) ?? "")
    .join("");
}

function toolUseNames(message: unknown): string[] {
  if (!isRec(message) || !Array.isArray(message.content)) return [];
  return message.content
    .filter((b): b is Rec => isRec(b) && b.type === "tool_use")
    .map((b) => str(b.name) ?? "?");
}

function denialLines(result: Rec): string[] {
  const denials = result.permission_denials;
  if (!Array.isArray(denials)) return [];
  return denials.filter(isRec).map((d) => {
    const tool = str(d.tool_name) ?? "tool";
    const input = d.tool_input === undefined ? "" : JSON.stringify(d.tool_input);
    return `${tool}(${input})`;
  });
}

function usageOf(result: Rec | undefined, pool: AuthMode): Usage {
  const u = result && isRec(result.usage) ? result.usage : {};
  const usage: Usage = {
    input: num(u.input_tokens) ?? 0,
    output: num(u.output_tokens) ?? 0,
    cache_read: num(u.cache_read_input_tokens) ?? 0,
    cache_write: num(u.cache_creation_input_tokens) ?? 0,
    pool,
  };
  const cost = result ? num(result.total_cost_usd) : undefined;
  if (cost !== undefined) usage.cost_usd = cost;
  return usage;
}

function clip(text: string): string {
  const t = text.trim();
  return t.length > ERROR_TEXT_MAX ? t.slice(0, ERROR_TEXT_MAX) : t;
}

export type ParserOpts = {
  pool: AuthMode;
  /** Called on every `result` event (drives stdin lifecycle in `startClaude`). */
  onResult?: (result: Rec) => void;
  now?: () => string;
};

export function createStreamParser(opts: ParserOpts): StreamParser {
  const lines = createLineSplitter();
  const now = opts.now ?? (() => new Date().toISOString());
  const snap: StreamSnapshot = { tools: 0, turns: 0, tool_uses: [], results: 0, denials: [] };
  let lastAssistantText = "";
  let result: Rec | undefined;

  const ingest = (line: string): RawEvent => {
    const ev: RawEvent = { ts: now(), harness: "claude", line };
    if (line.trim().length === 0) return ev;
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      return ev;
    }
    ev.parsed = parsed;
    if (!isRec(parsed)) return ev;
    const type = parsed.type;
    if (type === "system" && parsed.subtype === "init") {
      const sid = str(parsed.session_id);
      if (sid !== undefined) snap.session_id = sid;
      const model = str(parsed.model);
      if (model !== undefined) snap.model = model;
      snap.tools = Array.isArray(parsed.tools) ? parsed.tools.length : snap.tools;
    } else if (type === "assistant") {
      snap.turns += 1;
      snap.tool_uses.push(...toolUseNames(parsed.message));
      const text = assistantText(parsed.message);
      if (text.length > 0) lastAssistantText = text;
    } else if (type === "result") {
      result = parsed;
      snap.results += 1;
      snap.denials = denialLines(parsed);
      opts.onResult?.(parsed);
    }
    return ev;
  };

  const classify = (exit: SpawnExit): Pick<RunRes, "exit" | "error"> => {
    const stderr = exit.stderr;
    if (exit.timedOut) return { exit: "timeout", error: clip(stderr) || "timed out" };
    if (result) {
      if (result.subtype === "error_max_turns")
        return { exit: "max_turns", error: "max turns reached" };
      if (result.is_error === false) return { exit: "ok" };
      const resultText = str(result.result) ?? str(result.error) ?? "";
      const haystack = `${resultText}\n${stderr}`;
      const error = clip(resultText || stderr || `result subtype ${String(result.subtype)}`);
      if (RATE_LIMIT_RE.test(haystack)) return { exit: "rate_limited", error };
      if (AUTH_RE.test(haystack)) return { exit: "auth", error };
      return { exit: "error", error };
    }
    if (RATE_LIMIT_RE.test(stderr)) return { exit: "rate_limited", error: clip(stderr) };
    if (AUTH_RE.test(stderr)) return { exit: "auth", error: clip(stderr) };
    const detail =
      exit.error ??
      (stderr.trim()
        ? clip(stderr)
        : `no result event (exit code ${String(exit.code)}, signal ${String(exit.signal)})`);
    return { exit: "error", error: detail };
  };

  return {
    feed: (chunk) => lines.feed(chunk).map(ingest),
    snapshot: () => ({ ...snap, tool_uses: [...snap.tool_uses], denials: [...snap.denials] }),
    finish(exit) {
      lines.finish().forEach(ingest);
      const verdict = classify(exit);
      let text = (result && str(result.result)) ?? lastAssistantText;
      if (text.length === 0 && snap.denials.length > 0) {
        text = `permission denied: ${snap.denials.join(", ")}`;
      }
      const res: RunRes = { text, usage: usageOf(result, opts.pool), exit: verdict.exit };
      if (result && result.structured_output !== undefined) res.json = result.structured_output;
      const cursor = (result && str(result.session_id)) ?? snap.session_id;
      if (cursor !== undefined) res.cursor = cursor;
      if (verdict.error !== undefined) res.error = verdict.error;
      if (snap.denials.length > 0) {
        res.warnings = [
          `${snap.denials.length} permission denial(s): ${snap.denials.slice(0, 3).join(", ")}`,
        ];
      }
      return res;
    },
  };
}

// ---- process lifecycle ------------------------------------------------------------------------------

export type ClaudeRun = {
  pid: number;
  done: Promise<RunRes>;
  /** Send a mid-run user message over the open stdin (M2.6). Throws once stdin is closed. */
  nudge(text: string): void;
  kill(signal?: NodeJS.Signals): void;
  snapshot(): StreamSnapshot;
};

export type StartOpts = { bin?: string; parentEnv?: NodeJS.ProcessEnv };

export function startClaude(
  req: RunReq,
  onEvent: (e: RawEvent) => void,
  opts: StartOpts = {},
): ClaudeRun {
  // Every user message written to stdin owes one `result`; stdin ends when none is outstanding.
  let outstanding = 0;
  let stdinOpen = true;
  const proc = spawnClean(opts.bin ?? CLAUDE_BIN, buildArgv(req), {
    cwd: req.cwd,
    env: childEnv(req, opts.parentEnv),
    timeoutMs: req.timeout_ms,
  });
  const endStdin = (): void => {
    if (!stdinOpen) return;
    stdinOpen = false;
    proc.stdin.end();
  };
  const parser = createStreamParser({
    pool: req.auth,
    onResult: () => {
      outstanding = Math.max(0, outstanding - 1);
      if (outstanding === 0) endStdin();
    },
  });
  const send = (text: string): void => {
    if (!stdinOpen) throw new Error("claude stdin is closed");
    outstanding += 1;
    proc.stdin.write(userMessage(text));
  };
  proc.stdout.on("data", (chunk: string) => {
    for (const ev of parser.feed(chunk)) onEvent(ev);
  });
  const done = proc.exited.then((exit) => {
    stdinOpen = false;
    const res = parser.finish(exit);
    return res;
  });
  if (proc.pid > 0) send(req.prompt);
  return {
    pid: proc.pid,
    done,
    nudge: send,
    kill: (signal) => proc.kill(signal),
    snapshot: () => parser.snapshot(),
  };
}

// ---- adapter ---------------------------------------------------------------------------------------------

const PROBE_TIMEOUT_MS = 15_000;

export async function probeAuth(
  auth: AuthMode,
  opts: StartOpts = {},
): Promise<{ ok: boolean; login_cmd?: string }> {
  if (auth === "api-key") {
    const env = opts.parentEnv ?? process.env;
    return { ok: Boolean(env[CLAUDE_KEY_VAR]), login_cmd: `export ${CLAUDE_KEY_VAR}=...` };
  }
  const loggedIn = await new Promise<boolean>((resolve) => {
    execFile(
      opts.bin ?? CLAUDE_BIN,
      ["auth", "status"],
      { env: childEnv({ auth }, opts.parentEnv), timeout: PROBE_TIMEOUT_MS, encoding: "utf8" },
      (_err, stdout) => {
        try {
          const parsed: unknown = JSON.parse(String(stdout));
          resolve(isRec(parsed) && parsed.loggedIn === true);
        } catch {
          resolve(false);
        }
      },
    );
  });
  return { ok: loggedIn, login_cmd: "claude auth login" };
}

export const claudeAdapter: Adapter = {
  id: "claude",
  probeAuth: (auth) => probeAuth(auth),
  run: (req, onEvent) => startClaude(req, onEvent).done,
  effortMap,
};
