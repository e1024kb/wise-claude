// `gemini` harness adapter (research-ts-engine.md P6, D15; plan M5.3). Best effort: the CLI is
// installed here (0.46.0) but the user's login is broken (see probeAuth), so the parser is built
// from the shipped bundle's source and the `--help` text rather than a live run.
//
// Flags confirmed on gemini 0.46.0 (`gemini --help` plus the bundled source):
//   -p/--prompt <text>        headless mode; "appended to input on stdin (if any)"
//   -o/--output-format        text | json | stream-json
//   -m/--model <name>
//   --approval-mode           default | auto_edit | yolo | plan  (-y/--yolo = yolo)
//   --include-directories     extra workspace dirs (repeatable or comma-separated)
//   -r/--resume <id>          "latest", a 1-based index, or a full session UUID (per project cwd)
//   --session-id <uuid>       start a fresh session under a chosen id
//   --skip-trust              required headless in a dir the user never trusted (else exit 55)
//   no effort / thinking flag, no schema flag, no max-turns flag, no system-prompt flag
// stream-json events (one JSON object per line): `init {session_id, model}`, `message {role,
// content, delta?}`, `tool_use {tool_name, tool_id, parameters}`, `tool_result {tool_id, status,
// output, error?}`, `error` (either `{severity, message}` for a non-fatal notice or
// `{error: {type, message, code?}, stats}` for a fatal one), `result {status: "success", stats}`
// with `stats {total_tokens, input_tokens, output_tokens, cached, input, duration_ms,
// tool_calls, models}`. No cost field anywhere. Fatal exit codes: 41 auth, 42 input, 53 turn
// limit, 54 tool execution, 55 untrusted workspace, 130 cancelled; API errors reuse the HTTP
// status (429). An auth failure before the runner starts prints plain text on stderr and exits 1.
// Stdin: when it is not a TTY the CLI reads it (up to 8 MiB, giving up after 500 ms of silence)
// and uses it as the prompt, with any `-p` text appended. A prompt over `PROMPT_ARGV_MAX` bytes
// therefore travels on stdin with no `-p`; smaller prompts go on `-p` and stdin is closed at once.
// Schema: no flag exists, so the schema is appended to the prompt as an instruction and the JSON
// object is extracted from the final assistant text (fenced block or bare object, with a warning).
// Auth files: `$GEMINI_CLI_HOME/.gemini/oauth_creds.json` (default `~/.gemini`) for the OAuth
// login, `google_accounts.json` beside it; `GOOGLE_API_KEY` beats `GEMINI_API_KEY`. There is no
// login subcommand or status command: login happens in the interactive `gemini` via `/auth`.

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

export const GEMINI_BIN = "gemini";
/** Either key authenticates; the CLI prefers `GOOGLE_API_KEY` when both are set. */
export const GEMINI_KEY_VARS = ["GOOGLE_API_KEY", "GEMINI_API_KEY"] as const;
export const GEMINI_CONFIG_VAR = "GEMINI_CLI_HOME";
/** Copied when set: Code Assist accounts on a paid tier need the project id. */
export const GEMINI_KEEP_VARS = [GEMINI_CONFIG_VAR, "GOOGLE_CLOUD_PROJECT"] as const;

/**
 * D19: `yolo` only under `full-access`. Headless gemini cannot answer a confirmation, so under
 * `default` any tool that would ask is refused with a FatalToolExecutionError (exit 54).
 */
export const APPROVAL_MAP: Record<RunMode, string> = {
  "approval-required": "default",
  auto: "auto_edit",
  "full-access": "yolo",
};

export const RATE_LIMIT_RE = /rate.?limit|429|too many requests|quota|RESOURCE_EXHAUSTED/i;
export const AUTH_RE =
  /error authenticating|IneligibleTier|FatalAuthenticationError|not logged in|unauthenticated|unauthorized|401|invalid api key|API key not valid|credentials|log ?in|authenticate/i;
export const TURN_LIMIT_RE = /FatalTurnLimitedError|turn limit|max.?turns|session turns/i;
const ERROR_TEXT_MAX = 500;

// ---- argv ---------------------------------------------------------------------------------------

/** Prompts above this many bytes go on stdin (no `-p`) instead of argv. */
export const PROMPT_ARGV_MAX = 100_000;

export function promptViaStdin(prompt: string): boolean {
  return Buffer.byteLength(prompt, "utf8") > PROMPT_ARGV_MAX;
}

const SCHEMA_INSTRUCTION =
  "Respond with only a JSON object matching this JSON schema. No prose, no code fence, nothing before or after the object.";

/**
 * The prompt gemini receives: the system text has no flag, so it leads; the schema has no flag,
 * so it trails as an instruction (schema-by-instruction).
 */
export function composePrompt(req: Pick<RunReq, "prompt" | "system" | "schema">): string {
  const parts: string[] = [];
  if (req.system !== undefined) parts.push(req.system);
  parts.push(req.prompt);
  if (req.schema !== undefined) parts.push(`${SCHEMA_INSTRUCTION}\n${JSON.stringify(req.schema)}`);
  return parts.join("\n\n");
}

export function buildArgv(req: RunReq): string[] {
  const argv: string[] = [];
  const prompt = composePrompt(req);
  if (!promptViaStdin(prompt)) argv.push("-p", prompt);
  argv.push("--output-format", "stream-json", "--skip-trust");
  argv.push("--approval-mode", APPROVAL_MAP[req.mode]);
  for (const dir of req.add_dirs ?? []) argv.push("--include-directories", dir);
  if (req.model && req.model !== "inherit") argv.push("-m", req.model);
  // No effort, max-turns or schema flag on this CLI: `effortMap` is undefined for every level.
  if (typeof req.resume === "string" && req.resume.length > 0) argv.push("--resume", req.resume);
  return argv;
}

export function childEnv(
  req: Pick<RunReq, "auth" | "env">,
  parent?: NodeJS.ProcessEnv,
): Record<string, string> {
  // An API key in the environment beats the cached OAuth login inside gemini, so both key
  // variables travel only under `api-key`.
  return cleanEnv({
    ...(parent ? { parent } : {}),
    keep: GEMINI_KEEP_VARS,
    secrets: req.auth === "api-key" ? GEMINI_KEY_VARS : [],
    ...(req.env ? { extra: req.env } : {}),
  });
}

export function effortMap(e: Effort): string | undefined {
  return effortFor("gemini", e);
}

// ---- schema-by-instruction extraction -------------------------------------------------------------

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const num = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;

export type Extracted =
  | { ok: true; json: unknown; warning?: string }
  | { ok: false; error: string };

const FENCE_RE = /```(?:json|JSON)?\s*\n([\s\S]*?)\n\s*```/;

/** Index one past the `}` closing the object that opens at `start`, string-aware; -1 if none. */
function objectEnd(text: string, start: number): number {
  let depth = 0;
  let inString = false;
  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (ch === "\\") i++;
      else if (ch === '"') inString = false;
    } else if (ch === '"') inString = true;
    else if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return i + 1;
    }
  }
  return -1;
}

const tryParse = (s: string): { ok: true; value: unknown } | { ok: false } => {
  try {
    return { ok: true, value: JSON.parse(s) };
  } catch {
    return { ok: false };
  }
};

/**
 * The model was asked for a bare JSON object; accept the whole text, a fenced block, or the first
 * balanced `{…}` inside prose, warning when anything but the whole text parsed.
 */
export function extractJson(text: string): Extracted {
  const whole = tryParse(text.trim());
  if (whole.ok) return { ok: true, json: whole.value };
  const fence = FENCE_RE.exec(text);
  if (fence?.[1] !== undefined) {
    const inner = tryParse(fence[1].trim());
    if (inner.ok) {
      return { ok: true, json: inner.value, warning: "JSON extracted from a fenced code block" };
    }
  }
  let from = text.indexOf("{");
  while (from !== -1) {
    const end = objectEnd(text, from);
    if (end === -1) break;
    const bare = tryParse(text.slice(from, end));
    if (bare.ok) {
      return { ok: true, json: bare.value, warning: "JSON extracted from surrounding prose" };
    }
    from = text.indexOf("{", from + 1);
  }
  const shown = text.trim();
  return {
    ok: false,
    error: `final assistant text is not JSON: ${shown.length > ERROR_TEXT_MAX ? shown.slice(0, ERROR_TEXT_MAX) : shown || "(empty)"}`,
  };
}

// ---- stream parser ------------------------------------------------------------------------------

export type StreamSnapshot = {
  session_id?: string;
  model?: string;
  /** Assistant text segments so far; a segment closes at each tool result. */
  segments: number;
  tool_uses: number;
  /** A `result` event arrived. */
  result: boolean;
  errors: string[];
  warnings: string[];
};

export type StreamParser = {
  feed(chunk: string): RawEvent[];
  finish(exit: SpawnExit): RunRes;
  snapshot(): StreamSnapshot;
};

export type ParserOpts = {
  pool: AuthMode;
  /** A schema was given: the final assistant text must yield a JSON object. */
  expectJson?: boolean;
  now?: () => string;
};

/** stream-json `result.stats`; the per-model breakdown is ignored (totals are enough). */
function usageOf(stats: Rec | undefined, pool: AuthMode): Usage {
  const s = stats ?? {};
  return {
    input: num(s.input_tokens) ?? 0,
    output: num(s.output_tokens) ?? 0,
    cache_read: num(s.cached) ?? 0,
    cache_write: 0,
    pool,
  };
}

function clip(text: string): string {
  const t = text.trim();
  return t.length > ERROR_TEXT_MAX ? t.slice(0, ERROR_TEXT_MAX) : t;
}

/** Message of a fatal `error` event (`error: {type, message, code}`), tagged with its type. */
function fatalText(ev: Rec): string | undefined {
  if (!isRec(ev.error)) return undefined;
  const type = str(ev.error.type);
  const message = str(ev.error.message) ?? JSON.stringify(ev.error);
  const code = ev.error.code;
  const codeText = code === undefined ? "" : ` (code ${String(code)})`;
  return type ? `${type}: ${message}${codeText}` : `${message}${codeText}`;
}

export function createStreamParser(opts: ParserOpts): StreamParser {
  const lines = createLineSplitter();
  const now = opts.now ?? (() => new Date().toISOString());
  const snap: StreamSnapshot = {
    segments: 0,
    tool_uses: 0,
    result: false,
    errors: [],
    warnings: [],
  };
  const segments: string[] = [];
  let current = "";
  let stats: Rec | undefined;
  let status: string | undefined;
  let stdout = "";

  const closeSegment = (): void => {
    if (current.trim().length > 0) {
      segments.push(current);
      snap.segments = segments.length;
    }
    current = "";
  };

  const ingest = (line: string): RawEvent => {
    const ev: RawEvent = { ts: now(), harness: "gemini", line };
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
    if (type === "init") {
      const sid = str(parsed.session_id);
      if (sid !== undefined) snap.session_id = sid;
      const model = str(parsed.model);
      if (model !== undefined) snap.model = model;
    } else if (type === "message") {
      if (parsed.role === "assistant") current += str(parsed.content) ?? "";
    } else if (type === "tool_use") {
      snap.tool_uses += 1;
    } else if (type === "tool_result") {
      closeSegment();
    } else if (type === "error") {
      const fatal = fatalText(parsed);
      if (fatal !== undefined) {
        snap.errors.push(fatal);
        if (isRec(parsed.stats)) stats = parsed.stats;
      } else {
        const message = str(parsed.message) ?? "gemini error event";
        if (parsed.severity === "error") snap.errors.push(message);
        else snap.warnings.push(message);
      }
    } else if (type === "result") {
      snap.result = true;
      status = str(parsed.status);
      if (isRec(parsed.stats)) stats = parsed.stats;
    }
    return ev;
  };

  const classify = (exit: SpawnExit): Pick<RunRes, "exit" | "error"> => {
    const stderr = exit.stderr;
    if (exit.timedOut) return { exit: "timeout", error: clip(stderr) || "timed out" };
    if (snap.result && status === "success") return { exit: "ok" };
    const failure = snap.errors.at(-1);
    const haystack = `${snap.errors.join("\n")}\n${stderr}`;
    const detail =
      failure ??
      exit.error ??
      (stderr.trim()
        ? clip(stderr)
        : stdout.trim()
          ? `no result event (stdout: ${clip(stdout)})`
          : `no result event (exit code ${String(exit.code)}, signal ${String(exit.signal)})`);
    if (exit.code === 53 || TURN_LIMIT_RE.test(haystack)) {
      return { exit: "max_turns", error: clip(detail) };
    }
    if (exit.code === 41 || AUTH_RE.test(haystack)) return { exit: "auth", error: clip(detail) };
    if (RATE_LIMIT_RE.test(haystack)) return { exit: "rate_limited", error: clip(detail) };
    return { exit: "error", error: clip(detail) };
  };

  return {
    feed(chunk) {
      stdout += chunk;
      return lines.feed(chunk).map(ingest);
    },
    snapshot: () => ({ ...snap, errors: [...snap.errors], warnings: [...snap.warnings] }),
    finish(exit) {
      lines.finish().forEach(ingest);
      closeSegment();
      const verdict = classify(exit);
      const res: RunRes = {
        text: segments.at(-1) ?? "",
        usage: usageOf(stats, opts.pool),
        exit: verdict.exit,
      };
      if (snap.session_id !== undefined) res.cursor = snap.session_id;
      if (verdict.error !== undefined) res.error = verdict.error;
      const warnings = [...snap.warnings];
      if (opts.expectJson && verdict.exit === "ok") {
        const extracted = extractJson(res.text);
        if (extracted.ok) {
          res.json = extracted.json;
          if (extracted.warning !== undefined) warnings.push(extracted.warning);
        } else {
          res.exit = "error";
          res.error = extracted.error;
        }
      }
      if (warnings.length > 0) res.warnings = warnings;
      return res;
    },
  };
}

// ---- process lifecycle --------------------------------------------------------------------------

/** Same shape as `ClaudeRun` minus `nudge`: gemini reads stdin once, before the first turn. */
export type GeminiRun = {
  pid: number;
  done: Promise<RunRes>;
  kill(signal?: NodeJS.Signals): void;
  snapshot(): StreamSnapshot;
};

export type StartOpts = { bin?: string; parentEnv?: NodeJS.ProcessEnv; home?: string };

export function startGemini(
  req: RunReq,
  onEvent: (e: RawEvent) => void,
  opts: StartOpts = {},
): GeminiRun {
  const argv = buildArgv(req);
  const proc = spawnClean(opts.bin ?? GEMINI_BIN, argv, {
    cwd: req.cwd,
    env: childEnv(req, opts.parentEnv),
    timeoutMs: req.timeout_ms,
  });
  // The CLI reads a piped stdin as the prompt (giving up after 500 ms of silence): hand it the
  // whole prompt when argv would be too large, otherwise close it before the first byte.
  const prompt = composePrompt(req);
  if (promptViaStdin(prompt)) proc.stdin.end(prompt);
  else proc.stdin.end();
  const parser = createStreamParser({ pool: req.auth, expectJson: req.schema !== undefined });
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

/** `$GEMINI_CLI_HOME/.gemini/oauth_creds.json`, default `~/.gemini/oauth_creds.json`. */
export function authFilePath(env: NodeJS.ProcessEnv = process.env, home?: string): string {
  const root = env[GEMINI_CONFIG_VAR] ?? home ?? env.HOME ?? homedir();
  return join(root, ".gemini", "oauth_creds.json");
}

/**
 * gemini 0.46.0 has no login-status command, so the subscription probe is a file check: an
 * `oauth_creds.json` holding an access or refresh token counts as logged in. An unexpired token
 * on an ineligible account (this machine: "IneligibleTierError: This client is no longer
 * supported for Gemini Code Assist for individuals") passes the probe and fails the run with an
 * `auth` exit instead.
 */
export async function probeAuth(
  auth: AuthMode,
  opts: StartOpts = {},
): Promise<{ ok: boolean; login_cmd?: string }> {
  const env = opts.parentEnv ?? process.env;
  if (auth === "api-key") {
    const ok = GEMINI_KEY_VARS.some((v) => Boolean(env[v]));
    return { ok, login_cmd: `export ${GEMINI_KEY_VARS[1]}=...` };
  }
  let ok = false;
  try {
    const parsed: unknown = JSON.parse(readFileSync(authFilePath(env, opts.home), "utf8"));
    ok = isRec(parsed) && (Boolean(str(parsed.access_token)) || Boolean(str(parsed.refresh_token)));
  } catch {
    ok = false;
  }
  return { ok, login_cmd: "gemini (interactive, then /auth)" };
}

export const geminiAdapter: Adapter = {
  id: "gemini",
  bin: GEMINI_BIN,
  probeAuth: (auth) => probeAuth(auth),
  run: (req, onEvent) => startGemini(req, onEvent).done,
  effortMap,
};
