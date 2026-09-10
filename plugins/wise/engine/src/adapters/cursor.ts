// `cursor` harness adapter. Runs the explicit `cursor-agent` binary rather than the generic
// `agent` alias (which may belong to another provider) in headless stream-json mode.

import { execFile } from "node:child_process";
import { effortFor } from "../resolve.ts";
import type {
  Adapter,
  AuthMode,
  Effort,
  RawEvent,
  RunMode,
  RunReq,
  RunRes,
  Usage,
} from "../types.ts";
import { extractJson } from "./gemini.ts";
import { cleanEnv, createLineSplitter, spawnClean } from "./spawn.ts";
import type { SpawnExit } from "./spawn.ts";

export const CURSOR_BIN = "cursor-agent";
export const CURSOR_KEY_VAR = "CURSOR_API_KEY";
export const CURSOR_KEEP_VARS = ["CURSOR_CONFIG_DIR", "CURSOR_API_ENDPOINT"] as const;

export const RATE_LIMIT_RE = /rate.?limit|429|too many requests|usage limit|quota|exhausted/i;
export const AUTH_RE =
  /authentication required|not authenticated|not logged in|unauthenticated|unauthorized|401|invalid api key|token expired|agent login/i;
const ERROR_TEXT_MAX = 500;

const SCHEMA_INSTRUCTION =
  "Respond with only a JSON object matching this JSON schema. No prose, no code fence, nothing before or after the object.";

export function composePrompt(req: Pick<RunReq, "prompt" | "system" | "schema">): string {
  const parts: string[] = [];
  if (req.system !== undefined) parts.push(req.system);
  parts.push(req.prompt);
  if (req.schema !== undefined) parts.push(`${SCHEMA_INSTRUCTION}\n${JSON.stringify(req.schema)}`);
  return parts.join("\n\n");
}

/** Cursor's headless permission controls corresponding to wise's three run modes. */
export const MODE_ARGS: Record<RunMode, readonly string[]> = {
  "approval-required": ["--mode", "ask", "--sandbox", "enabled"],
  auto: ["--force", "--sandbox", "enabled"],
  "full-access": ["--force", "--sandbox", "disabled", "--approve-mcps"],
};

export function buildArgv(req: RunReq): string[] {
  const argv = [
    "--print",
    "--output-format",
    "stream-json",
    "--stream-partial-output",
    "--trust",
    "--workspace",
    req.cwd,
    ...MODE_ARGS[req.mode],
  ];
  if (req.model && req.model !== "inherit") argv.push("--model", req.model);
  if (typeof req.resume === "string" && req.resume.length > 0) argv.push("--resume", req.resume);
  for (const dir of req.add_dirs ?? []) argv.push("--add-dir", dir);
  return argv;
}

export function childEnv(
  req: Pick<RunReq, "auth" | "env">,
  parent?: NodeJS.ProcessEnv,
): Record<string, string> {
  return cleanEnv({
    ...(parent ? { parent } : {}),
    keep: CURSOR_KEEP_VARS,
    secrets: req.auth === "api-key" ? [CURSOR_KEY_VAR] : [],
    ...(req.env ? { extra: req.env } : {}),
  });
}

export function effortMap(e: Effort): string | undefined {
  return effortFor("cursor", e);
}

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);

function emptyUsage(pool: AuthMode): Usage {
  return { input: 0, output: 0, cache_read: 0, cache_write: 0, pool };
}

function clip(text: string): string {
  const t = text.trim();
  return t.length > ERROR_TEXT_MAX ? t.slice(0, ERROR_TEXT_MAX) : t;
}

function assistantDelta(message: unknown): string {
  if (!isRec(message) || !Array.isArray(message.content)) return "";
  return message.content
    .filter((part): part is Rec => isRec(part) && part.type === "text")
    .map((part) => str(part.text) ?? "")
    .join("");
}

function resultError(result: Rec): string {
  const direct = str(result.error) ?? str(result.result);
  if (direct) return direct;
  return `cursor result subtype ${String(result.subtype)}`;
}

export type StreamSnapshot = {
  session_id?: string;
  model?: string;
  assistant_events: number;
  tool_calls: number;
  results: number;
  errors: string[];
};

export type StreamParser = {
  feed(chunk: string): RawEvent[];
  finish(exit: SpawnExit): RunRes;
  snapshot(): StreamSnapshot;
};

export type ParserOpts = {
  pool: AuthMode;
  expectJson?: boolean;
  now?: () => string;
};

export function createStreamParser(opts: ParserOpts): StreamParser {
  const lines = createLineSplitter();
  const now = opts.now ?? (() => new Date().toISOString());
  const snap: StreamSnapshot = { assistant_events: 0, tool_calls: 0, results: 0, errors: [] };
  let assistant = "";
  let terminal: Rec | undefined;

  const ingest = (line: string): RawEvent => {
    const ev: RawEvent = { ts: now(), harness: "cursor", line };
    if (line.trim().length === 0) return ev;
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      return ev;
    }
    ev.parsed = parsed;
    if (!isRec(parsed)) return ev;
    if (parsed.type === "system" && parsed.subtype === "init") {
      const session = str(parsed.session_id);
      const model = str(parsed.model);
      if (session !== undefined) snap.session_id = session;
      if (model !== undefined) snap.model = model;
    } else if (parsed.type === "assistant") {
      snap.assistant_events += 1;
      assistant += assistantDelta(parsed.message);
    } else if (parsed.type === "tool_call") {
      if (parsed.subtype === "started") snap.tool_calls += 1;
    } else if (parsed.type === "result") {
      terminal = parsed;
      snap.results += 1;
      const session = str(parsed.session_id);
      if (session !== undefined) snap.session_id = session;
      if (parsed.is_error === true || parsed.subtype !== "success") {
        snap.errors.push(resultError(parsed));
      }
    } else if (parsed.type === "error") {
      const message = str(parsed.message) ?? str(parsed.error) ?? JSON.stringify(parsed);
      snap.errors.push(message);
    }
    return ev;
  };

  const classify = (exit: SpawnExit): Pick<RunRes, "exit" | "error"> => {
    if (exit.timedOut) return { exit: "timeout", error: clip(exit.stderr) || "timed out" };
    const failure = snap.errors.at(-1);
    const haystack = `${snap.errors.join("\n")}\n${exit.stderr}`;
    if (
      failure !== undefined ||
      terminal === undefined ||
      (exit.code !== null && exit.code !== 0)
    ) {
      const detail =
        failure ??
        exit.error ??
        (exit.stderr.trim()
          ? clip(exit.stderr)
          : `no successful result event (exit code ${String(exit.code)}, signal ${String(exit.signal)})`);
      if (RATE_LIMIT_RE.test(haystack)) return { exit: "rate_limited", error: clip(detail) };
      if (AUTH_RE.test(haystack)) return { exit: "auth", error: clip(detail) };
      return { exit: "error", error: clip(detail) };
    }
    return { exit: "ok" };
  };

  return {
    feed: (chunk) => lines.feed(chunk).map(ingest),
    snapshot: () => ({ ...snap, errors: [...snap.errors] }),
    finish(exit) {
      lines.finish().forEach(ingest);
      const verdict = classify(exit);
      const text = terminal ? (str(terminal.result) ?? assistant) : assistant;
      const res: RunRes = { text, usage: emptyUsage(opts.pool), exit: verdict.exit };
      if (snap.session_id !== undefined) res.cursor = snap.session_id;
      if (snap.model !== undefined) res.model = snap.model;
      if (verdict.error !== undefined) res.error = verdict.error;
      if (opts.expectJson && verdict.exit === "ok") {
        const extracted = extractJson(text);
        if (extracted.ok) {
          res.json = extracted.json;
          if (extracted.warning) res.warnings = [extracted.warning];
        } else {
          res.exit = "error";
          res.error = extracted.error;
        }
      }
      return res;
    },
  };
}

export type CursorRun = {
  pid: number;
  done: Promise<RunRes>;
  kill(signal?: NodeJS.Signals): void;
  snapshot(): StreamSnapshot;
};

export type StartOpts = { bin?: string; parentEnv?: NodeJS.ProcessEnv };

export function startCursor(
  req: RunReq,
  onEvent: (e: RawEvent) => void,
  opts: StartOpts = {},
): CursorRun {
  const proc = spawnClean(opts.bin ?? CURSOR_BIN, buildArgv(req), {
    cwd: req.cwd,
    env: childEnv(req, opts.parentEnv),
    timeoutMs: req.timeout_ms,
  });
  proc.stdin.end(composePrompt(req));
  const parser = createStreamParser({ pool: req.auth, expectJson: req.schema !== undefined });
  proc.stdout.on("data", (chunk: string) => {
    for (const ev of parser.feed(chunk)) onEvent(ev);
  });
  const done = proc.exited.then((exit) => {
    const res = parser.finish(exit);
    const warnings = [...(res.warnings ?? [])];
    if (req.resume !== undefined && typeof req.resume !== "string") {
      warnings.push("ignored non-string resume cursor");
    }
    if (req.max_turns !== undefined) warnings.push("cursor does not support max_turns");
    if (warnings.length > 0) res.warnings = warnings;
    return res;
  });
  return {
    pid: proc.pid,
    done,
    kill: (signal) => proc.kill(signal),
    snapshot: () => parser.snapshot(),
  };
}

const PROBE_TIMEOUT_MS = 15_000;

export async function probeAuth(
  auth: AuthMode,
  opts: StartOpts = {},
): Promise<{ ok: boolean; login_cmd?: string }> {
  if (auth === "api-key") {
    const env = opts.parentEnv ?? process.env;
    return { ok: Boolean(env[CURSOR_KEY_VAR]), login_cmd: `export ${CURSOR_KEY_VAR}=...` };
  }
  const loggedIn = await new Promise<boolean>((resolve) => {
    execFile(
      opts.bin ?? CURSOR_BIN,
      ["status", "--format", "json"],
      { env: childEnv({ auth }, opts.parentEnv), timeout: PROBE_TIMEOUT_MS, encoding: "utf8" },
      (err, stdout) => {
        if (err !== null) return resolve(false);
        try {
          const status = JSON.parse(String(stdout)) as Rec;
          resolve(status.isAuthenticated === true);
        } catch {
          resolve(false);
        }
      },
    );
  });
  return { ok: loggedIn, login_cmd: "cursor-agent login" };
}

export const cursorAdapter: Adapter = {
  id: "cursor",
  bin: CURSOR_BIN,
  probeAuth: (auth) => probeAuth(auth),
  run: (req, onEvent) => startCursor(req, onEvent).done,
  effortMap,
};
