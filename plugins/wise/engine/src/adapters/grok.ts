// `grok` harness adapter (research-ts-engine.md P6, D15, D19; M0.4 result shape).
// Runs `grok -p <prompt> --output-format json --json-schema <json>` on the unmodified binary.
// Headless grok prints one JSON document on stdout when the turn ends (pretty-printed, so it
// spans lines); the parser buffers stdout and reads the document at exit, falling back to the
// last JSON line for NDJSON output formats. No open stdin, so no nudge: kill + `--resume`.

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
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
import { cleanEnv, createLineSplitter, spawnClean } from "./spawn.ts";
import type { SpawnExit } from "./spawn.ts";

export const GROK_BIN = "grok";
export const GROK_KEY_VAR = "XAI_API_KEY";
export const GROK_CONFIG_VAR = "GROK_HOME";

/**
 * D19: `--always-approve` only under `full-access`. Headless grok cannot answer a prompt, so
 * `approval-required` uses `dontAsk` (anything not pre-granted via `--allow` is denied) and
 * `auto` uses `acceptEdits`, the same meaning as the Claude map.
 */
export const PERMISSION_MAP: Record<RunMode, readonly string[]> = {
  "approval-required": ["--permission-mode", "dontAsk"],
  auto: ["--permission-mode", "acceptEdits"],
  "full-access": ["--always-approve"],
};

export const RATE_LIMIT_RE = /rate.?limit|429|too many requests|quota exceeded/i;
export const AUTH_RE =
  /GROK_AUTH_EXPIRED|not logged in|grok login|unauthorized|401|re-?authenticat|token expired|sign in/i;
const ERROR_TEXT_MAX = 500;

// ---- argv ---------------------------------------------------------------------------------------

export function buildArgv(req: RunReq): string[] {
  const argv = ["-p", req.prompt, "--output-format", "json", "--no-auto-update", "--cwd", req.cwd];
  argv.push(...PERMISSION_MAP[req.mode]);
  for (const rule of req.allowed_tools ?? []) argv.push("--allow", rule);
  if (req.model && req.model !== "inherit") argv.push("-m", req.model);
  const effort = req.effort === undefined ? undefined : effortMap(req.effort);
  if (effort !== undefined) argv.push("--reasoning-effort", effort);
  if (req.schema !== undefined) argv.push("--json-schema", JSON.stringify(req.schema));
  if (req.max_turns !== undefined) argv.push("--max-turns", String(req.max_turns));
  if (typeof req.resume === "string" && req.resume.length > 0) argv.push("--resume", req.resume);
  // `--rules` appends to the system prompt; `--system-prompt-override` would replace it.
  if (req.system !== undefined) argv.push("--rules", req.system);
  return argv;
}

export function childEnv(
  req: Pick<RunReq, "auth" | "env">,
  parent?: NodeJS.ProcessEnv,
): Record<string, string> {
  // XAI_API_KEY beats the cached login inside grok, so it travels only under `api-key`.
  return cleanEnv({
    ...(parent ? { parent } : {}),
    keep: [GROK_CONFIG_VAR],
    secrets: req.auth === "api-key" ? [GROK_KEY_VAR] : [],
    ...(req.env ? { extra: req.env } : {}),
  });
}

export function effortMap(e: Effort): string | undefined {
  return effortFor("grok", e);
}

// ---- result parser ------------------------------------------------------------------------------

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const num = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;

export type StreamSnapshot = {
  session_id?: string;
  model?: string;
  lines: number;
  /** A complete result document was found. */
  result: boolean;
};

export type StreamParser = {
  feed(chunk: string): RawEvent[];
  finish(exit: SpawnExit): RunRes;
  snapshot(): StreamSnapshot;
};

export type ParserOpts = { pool: AuthMode; now?: () => string };

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

/** A result document carries `text`, `sessionId` or `usage`; NDJSON `end` events carry `type`. */
function looksLikeResult(v: unknown): v is Rec {
  return (
    isRec(v) &&
    (v.text !== undefined ||
      v.sessionId !== undefined ||
      v.usage !== undefined ||
      v.structuredOutput !== undefined ||
      v.error !== undefined)
  );
}

function resultError(result: Rec): string | undefined {
  if (result.is_error === true || result.isError === true) {
    return str(result.text) ?? str(result.error) ?? "grok reported an error";
  }
  if (isRec(result.error)) return str(result.error.message) ?? JSON.stringify(result.error);
  return str(result.error);
}

export function createStreamParser(opts: ParserOpts): StreamParser {
  const lines = createLineSplitter();
  const now = opts.now ?? (() => new Date().toISOString());
  const snap: StreamSnapshot = { lines: 0, result: false };
  let buffer = "";
  let lastLineResult: Rec | undefined;

  const ingest = (line: string): RawEvent => {
    snap.lines += 1;
    const ev: RawEvent = { ts: now(), harness: "grok", line };
    if (line.trim().length === 0) return ev;
    // One-line documents (NDJSON formats) parse per line; the pretty-printed default does not.
    try {
      const parsed: unknown = JSON.parse(line);
      ev.parsed = parsed;
      if (looksLikeResult(parsed)) {
        lastLineResult = parsed;
        const sid = str(parsed.sessionId);
        if (sid !== undefined) snap.session_id = sid;
      }
    } catch {
      // Part of a multi-line document; resolved in finish().
    }
    return ev;
  };

  const readResult = (): Rec | undefined => {
    const whole = buffer.trim();
    if (whole.length > 0) {
      try {
        const parsed: unknown = JSON.parse(whole);
        if (looksLikeResult(parsed)) return parsed;
      } catch {
        // Not one document; fall back to the last JSON line.
      }
    }
    return lastLineResult;
  };

  const classify = (exit: SpawnExit, result: Rec | undefined): Pick<RunRes, "exit" | "error"> => {
    const stderr = exit.stderr;
    if (exit.timedOut) return { exit: "timeout", error: clip(stderr) || "timed out" };
    if (result) {
      const stop = str(result.stopReason) ?? "";
      if (/max.?turns/i.test(stop)) return { exit: "max_turns", error: "max turns reached" };
      const failure = resultError(result);
      if (failure === undefined) return { exit: "ok" };
      const haystack = `${failure}\n${stderr}`;
      if (RATE_LIMIT_RE.test(haystack)) return { exit: "rate_limited", error: clip(failure) };
      if (AUTH_RE.test(haystack)) return { exit: "auth", error: clip(failure) };
      return { exit: "error", error: clip(failure) };
    }
    const tail = `${stderr}\n${buffer}`;
    if (RATE_LIMIT_RE.test(tail)) return { exit: "rate_limited", error: clip(stderr || buffer) };
    if (AUTH_RE.test(tail)) return { exit: "auth", error: clip(stderr || buffer) };
    const detail =
      exit.error ??
      (stderr.trim()
        ? clip(stderr)
        : buffer.trim()
          ? `no JSON result (stdout: ${clip(buffer)})`
          : `no JSON result (exit code ${String(exit.code)}, signal ${String(exit.signal)})`);
    return { exit: "error", error: detail };
  };

  return {
    feed(chunk) {
      buffer += chunk;
      return lines.feed(chunk).map(ingest);
    },
    snapshot: () => ({ ...snap }),
    finish(exit) {
      lines.finish().forEach(ingest);
      const result = readResult();
      if (result) {
        snap.result = true;
        const sid = str(result.sessionId);
        if (sid !== undefined) snap.session_id = sid;
        const models = isRec(result.modelUsage) ? Object.keys(result.modelUsage) : [];
        if (models[0] !== undefined) snap.model = models[0];
      }
      const verdict = classify(exit, result);
      const res: RunRes = {
        text: (result && str(result.text)) ?? "",
        usage: usageOf(result, opts.pool),
        exit: verdict.exit,
      };
      if (result && result.structuredOutput !== undefined) res.json = result.structuredOutput;
      if (snap.session_id !== undefined) res.cursor = snap.session_id;
      if (verdict.error !== undefined) res.error = verdict.error;
      return res;
    },
  };
}

// ---- process lifecycle --------------------------------------------------------------------------

/** Same shape as `ClaudeRun` minus `nudge`: grok `-p` has no open stdin. */
export type GrokRun = {
  pid: number;
  done: Promise<RunRes>;
  kill(signal?: NodeJS.Signals): void;
  snapshot(): StreamSnapshot;
};

export type StartOpts = { bin?: string; parentEnv?: NodeJS.ProcessEnv; home?: string };

export function startGrok(
  req: RunReq,
  onEvent: (e: RawEvent) => void,
  opts: StartOpts = {},
): GrokRun {
  const proc = spawnClean(opts.bin ?? GROK_BIN, buildArgv(req), {
    cwd: req.cwd,
    env: childEnv(req, opts.parentEnv),
    timeoutMs: req.timeout_ms,
  });
  proc.stdin.end();
  const parser = createStreamParser({ pool: req.auth });
  proc.stdout.on("data", (chunk: string) => {
    for (const ev of parser.feed(chunk)) onEvent(ev);
  });
  const done = proc.exited.then((exit) => {
    const res = parser.finish(exit);
    if (req.resume !== undefined && typeof req.resume !== "string") {
      res.warnings = [...(res.warnings ?? []), "ignored non-string resume cursor"];
    }
    return res;
  });
  return {
    pid: proc.pid,
    done,
    kill: (signal) => proc.kill(signal),
    snapshot: () => parser.snapshot(),
  };
}

// ---- adapter ------------------------------------------------------------------------------------

/** `$GROK_HOME/auth.json`, default `~/.grok/auth.json`. */
export function authFilePath(env: NodeJS.ProcessEnv = process.env, home?: string): string {
  const root = env[GROK_CONFIG_VAR] ?? join(home ?? env.HOME ?? homedir(), ".grok");
  return join(root, "auth.json");
}

/**
 * grok 1.0.5 has no login-status command, so the subscription probe is a file check: a
 * non-empty JSON object at `auth.json` counts as logged in. An expired token is only seen at
 * run time, as an `auth` exit.
 */
export async function probeAuth(
  auth: AuthMode,
  opts: StartOpts = {},
): Promise<{ ok: boolean; login_cmd?: string }> {
  const env = opts.parentEnv ?? process.env;
  if (auth === "api-key") {
    return { ok: Boolean(env[GROK_KEY_VAR]), login_cmd: `export ${GROK_KEY_VAR}=...` };
  }
  let ok = false;
  try {
    const parsed: unknown = JSON.parse(readFileSync(authFilePath(env, opts.home), "utf8"));
    ok = isRec(parsed) && Object.keys(parsed).length > 0;
  } catch {
    ok = false;
  }
  return { ok, login_cmd: "grok login" };
}

export const grokAdapter: Adapter = {
  id: "grok",
  probeAuth: (auth) => probeAuth(auth),
  run: (req, onEvent) => startGrok(req, onEvent).done,
  effortMap,
};
