// `codex` harness adapter (research-ts-engine.md P6, D15, D19; M0.3 event shapes).
// Runs `codex exec --json --output-schema <file>` (or `codex exec resume <thread>`) on the
// unmodified binary. The prompt is a positional argument and stdin is closed at once (the CLI
// otherwise waits on it); a prompt over `PROMPT_ARGV_MAX` bytes travels on stdin instead, with
// `-` in its place, so E5 diffs never hit ARG_MAX. Stdin carries nothing else, so a running
// child cannot be nudged: the engine kills and resumes via the thread id instead (D16).

import { execFile } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { effortFor } from "../resolve.ts";
import type {
  Adapter,
  AuthMode,
  Effort,
  JsonSchema,
  RawEvent,
  RunMode,
  RunReq,
  RunRes,
  Usage,
} from "../types.ts";
import { cleanEnv, createLineSplitter, spawnClean } from "./spawn.ts";
import type { SpawnExit } from "./spawn.ts";

export const CODEX_BIN = "codex";
export const CODEX_KEY_VAR = "OPENAI_API_KEY";
export const CODEX_CONFIG_VAR = "CODEX_HOME";

/** D19: for codex the sandbox flag is the permission model. */
export const SANDBOX_MAP: Record<RunMode, string> = {
  "approval-required": "read-only",
  auto: "workspace-write",
  "full-access": "danger-full-access",
};

export const RATE_LIMIT_RE = /rate.?limit|429|too many requests|usage limit|quota/i;
export const AUTH_RE =
  /not logged in|codex login|unauthorized|401|invalid api key|authentication failed|token expired/i;
const ERROR_TEXT_MAX = 500;

// ---- argv ---------------------------------------------------------------------------------------

/** TOML string literal for a `-c key=value` override. */
const toml = (v: string): string => JSON.stringify(v);

export type ArgvOpts = {
  /** Path of the schema file written for this run (`--output-schema` takes a path, not JSON). */
  schemaPath?: string;
};

/** The prompt codex receives: the system text has no flag of its own, so it leads the prompt. */
export function composePrompt(req: Pick<RunReq, "prompt" | "system">): string {
  return req.system === undefined ? req.prompt : `${req.system}\n\n${req.prompt}`;
}

/** Prompts above this many bytes go over stdin (`codex exec -`) instead of argv. */
export const PROMPT_ARGV_MAX = 100_000;

export function promptViaStdin(prompt: string): boolean {
  return Buffer.byteLength(prompt, "utf8") > PROMPT_ARGV_MAX;
}

export function buildArgv(req: RunReq, opts: ArgvOpts = {}): string[] {
  const resume = typeof req.resume === "string" && req.resume.length > 0 ? req.resume : undefined;
  const argv = resume === undefined ? ["exec"] : ["exec", "resume", resume];
  argv.push("--json", "--skip-git-repo-check");
  if (resume === undefined) {
    // `-C`, `-s` and `--add-dir` exist on `exec` only; the resume form takes config overrides.
    argv.push("-C", req.cwd, "-s", SANDBOX_MAP[req.mode]);
    for (const dir of req.add_dirs ?? []) argv.push("--add-dir", dir);
  } else {
    argv.push("-c", `sandbox_mode=${toml(SANDBOX_MAP[req.mode])}`);
    if (req.add_dirs && req.add_dirs.length > 0) {
      argv.push(
        "-c",
        `sandbox_workspace_write.writable_roots=[${req.add_dirs.map(toml).join(",")}]`,
      );
    }
  }
  // Headless: never wait on an approval prompt.
  argv.push("-c", `approval_policy=${toml("never")}`);
  const effort = req.effort === undefined ? undefined : effortMap(req.effort);
  if (effort !== undefined) argv.push("-c", `model_reasoning_effort=${toml(effort)}`);
  if (req.model && req.model !== "inherit") argv.push("-m", req.model);
  if (req.schema !== undefined) {
    if (opts.schemaPath === undefined) throw new Error("codex: schema given without schemaPath");
    argv.push("--output-schema", opts.schemaPath);
  }
  const prompt = composePrompt(req);
  argv.push(promptViaStdin(prompt) ? "-" : prompt);
  return argv;
}

/**
 * Codex validates `--output-schema` in OpenAI strict mode: every object needs
 * `additionalProperties: false` and a `required` list naming every property. Workflow schemas
 * are written for Claude's lenient `--json-schema`, so the adapter tightens a copy: a property
 * the author left optional becomes required but nullable, which keeps its meaning.
 */
export function strictSchema(schema: JsonSchema): JsonSchema {
  return tighten(schema) as JsonSchema;
}

function tighten(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(tighten);
  if (!isRec(node)) return node;
  const out: Rec = {};
  for (const [k, v] of Object.entries(node)) {
    if (k === "properties" || k === "$defs" || k === "definitions") {
      out[k] = isRec(v)
        ? Object.fromEntries(Object.entries(v).map(([n, s]) => [n, tighten(s)]))
        : v;
    } else if (k === "items" || k === "anyOf" || k === "oneOf" || k === "allOf" || k === "not") {
      out[k] = tighten(v);
    } else {
      out[k] = v;
    }
  }
  const props = isRec(out.properties) ? out.properties : undefined;
  if (out.type === "object" || props !== undefined) {
    if (out.additionalProperties === undefined) out.additionalProperties = false;
    if (props !== undefined) {
      const required = new Set(
        Array.isArray(out.required) ? out.required.filter((r) => typeof r === "string") : [],
      );
      for (const [name, spec] of Object.entries(props)) {
        if (required.has(name)) continue;
        required.add(name);
        props[name] = nullable(spec);
      }
      out.required = Object.keys(props).filter((n) => required.has(n));
    }
  }
  return out;
}

/** Allow `null` for a property that was optional before `tighten` made it required. */
function nullable(spec: unknown): unknown {
  if (!isRec(spec)) return spec;
  const t = spec.type;
  if (typeof t === "string" && t !== "null") return { ...spec, type: [t, "null"] };
  if (Array.isArray(t) && !t.includes("null")) return { ...spec, type: [...t, "null"] };
  return spec;
}

/** Write the schema to a fresh temp dir; `cleanup` removes the dir. */
export function writeSchemaFile(schema: JsonSchema): { path: string; cleanup(): void } {
  const dir = mkdtempSync(join(tmpdir(), "wise-codex-"));
  const path = join(dir, "schema.json");
  writeFileSync(path, JSON.stringify(schema));
  return {
    path,
    cleanup: () => {
      try {
        rmSync(dir, { recursive: true, force: true });
      } catch {
        // Best effort.
      }
    },
  };
}

export function childEnv(
  req: Pick<RunReq, "auth" | "env">,
  parent?: NodeJS.ProcessEnv,
): Record<string, string> {
  return cleanEnv({
    ...(parent ? { parent } : {}),
    keep: [CODEX_CONFIG_VAR],
    secrets: req.auth === "api-key" ? [CODEX_KEY_VAR] : [],
    ...(req.env ? { extra: req.env } : {}),
  });
}

export function effortMap(e: Effort): string | undefined {
  return effortFor("codex", e);
}

// ---- stream parser ------------------------------------------------------------------------------

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const num = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;

export type StreamSnapshot = {
  thread_id?: string;
  turns: number;
  /** Completed turns (`turn.completed`). */
  completed: number;
  commands: string[];
  file_changes: number;
  errors: string[];
};

export type StreamParser = {
  feed(chunk: string): RawEvent[];
  finish(exit: SpawnExit): RunRes;
  snapshot(): StreamSnapshot;
};

export type ParserOpts = {
  pool: AuthMode;
  /** A schema was given: the final agent message must parse as JSON. */
  expectJson?: boolean;
  now?: () => string;
};

function usageOf(u: Rec | undefined, pool: AuthMode): Usage {
  const src = u ?? {};
  return {
    input: num(src.input_tokens) ?? 0,
    output: num(src.output_tokens) ?? 0,
    cache_read: num(src.cached_input_tokens) ?? 0,
    cache_write: num(src.cache_write_input_tokens) ?? 0,
    pool,
  };
}

function clip(text: string): string {
  const t = text.trim();
  return t.length > ERROR_TEXT_MAX ? t.slice(0, ERROR_TEXT_MAX) : t;
}

/** Error text from a `turn.failed` or `error` event. */
function errorText(ev: Rec): string | undefined {
  if (isRec(ev.error)) return str(ev.error.message) ?? JSON.stringify(ev.error);
  return str(ev.message) ?? str(ev.error);
}

export function createStreamParser(opts: ParserOpts): StreamParser {
  const lines = createLineSplitter();
  const now = opts.now ?? (() => new Date().toISOString());
  const snap: StreamSnapshot = {
    turns: 0,
    completed: 0,
    commands: [],
    file_changes: 0,
    errors: [],
  };
  let lastAgentText = "";
  let usage: Rec | undefined;

  const ingest = (line: string): RawEvent => {
    const ev: RawEvent = { ts: now(), harness: "codex", line };
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
    if (type === "thread.started") {
      const id = str(parsed.thread_id);
      if (id !== undefined) snap.thread_id = id;
    } else if (type === "turn.started") {
      snap.turns += 1;
    } else if (type === "turn.completed") {
      snap.completed += 1;
      if (isRec(parsed.usage)) usage = parsed.usage;
    } else if (type === "turn.failed" || type === "error") {
      snap.errors.push(errorText(parsed) ?? `codex ${String(type)}`);
    } else if (type === "item.completed" && isRec(parsed.item)) {
      const item = parsed.item;
      if (item.type === "agent_message") {
        const text = str(item.text);
        if (text !== undefined && text.length > 0) lastAgentText = text;
      } else if (item.type === "command_execution") {
        snap.commands.push(str(item.command) ?? "?");
      } else if (item.type === "file_change") {
        snap.file_changes += Array.isArray(item.changes) ? item.changes.length : 1;
      } else if (item.type === "error") {
        snap.errors.push(str(item.message) ?? "codex item error");
      }
    }
    return ev;
  };

  const classify = (exit: SpawnExit): Pick<RunRes, "exit" | "error"> => {
    const stderr = exit.stderr;
    if (exit.timedOut) return { exit: "timeout", error: clip(stderr) || "timed out" };
    const failure = snap.errors.at(-1);
    const haystack = `${snap.errors.join("\n")}\n${stderr}`;
    if (failure !== undefined || snap.completed === 0 || (exit.code !== null && exit.code !== 0)) {
      const detail =
        failure ??
        exit.error ??
        (stderr.trim()
          ? clip(stderr)
          : `no turn.completed event (exit code ${String(exit.code)}, signal ${String(exit.signal)})`);
      if (RATE_LIMIT_RE.test(haystack)) return { exit: "rate_limited", error: clip(detail) };
      if (AUTH_RE.test(haystack)) return { exit: "auth", error: clip(detail) };
      return { exit: "error", error: clip(detail) };
    }
    return { exit: "ok" };
  };

  return {
    feed: (chunk) => lines.feed(chunk).map(ingest),
    snapshot: () => ({ ...snap, commands: [...snap.commands], errors: [...snap.errors] }),
    finish(exit) {
      lines.finish().forEach(ingest);
      const verdict = classify(exit);
      const res: RunRes = {
        text: lastAgentText,
        usage: usageOf(usage, opts.pool),
        exit: verdict.exit,
      };
      if (snap.thread_id !== undefined) res.cursor = snap.thread_id;
      if (verdict.error !== undefined) res.error = verdict.error;
      if (opts.expectJson && verdict.exit === "ok") {
        try {
          res.json = JSON.parse(lastAgentText);
        } catch {
          res.exit = "error";
          res.error = `final agent message is not JSON: ${clip(lastAgentText) || "(empty)"}`;
        }
      }
      return res;
    },
  };
}

// ---- process lifecycle --------------------------------------------------------------------------

/** Same shape as `ClaudeRun` minus `nudge`: codex has no open stdin. */
export type CodexRun = {
  pid: number;
  done: Promise<RunRes>;
  kill(signal?: NodeJS.Signals): void;
  snapshot(): StreamSnapshot;
};

export type StartOpts = { bin?: string; parentEnv?: NodeJS.ProcessEnv };

export function startCodex(
  req: RunReq,
  onEvent: (e: RawEvent) => void,
  opts: StartOpts = {},
): CodexRun {
  const schemaFile =
    req.schema === undefined ? undefined : writeSchemaFile(strictSchema(req.schema));
  const argv = buildArgv(req, schemaFile ? { schemaPath: schemaFile.path } : {});
  const proc = spawnClean(opts.bin ?? CODEX_BIN, argv, {
    cwd: req.cwd,
    env: childEnv(req, opts.parentEnv),
    timeoutMs: req.timeout_ms,
  });
  // Codex reads stdin until EOF when it is a pipe: close it before the first byte, or hand it
  // the whole prompt when argv would be too large.
  const prompt = composePrompt(req);
  if (promptViaStdin(prompt)) proc.stdin.end(prompt);
  else proc.stdin.end();
  const parser = createStreamParser({ pool: req.auth, expectJson: req.schema !== undefined });
  proc.stdout.on("data", (chunk: string) => {
    for (const ev of parser.feed(chunk)) onEvent(ev);
  });
  const done = proc.exited.then((exit) => {
    schemaFile?.cleanup();
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

const PROBE_TIMEOUT_MS = 15_000;
export const LOGGED_IN_RE = /logged in/i;

export async function probeAuth(
  auth: AuthMode,
  opts: StartOpts = {},
): Promise<{ ok: boolean; login_cmd?: string }> {
  if (auth === "api-key") {
    const env = opts.parentEnv ?? process.env;
    return { ok: Boolean(env[CODEX_KEY_VAR]), login_cmd: `export ${CODEX_KEY_VAR}=...` };
  }
  // `codex login status` prints "Logged in using ChatGPT" (exit 0) or "Not logged in" (exit 1).
  const loggedIn = await new Promise<boolean>((resolve) => {
    execFile(
      opts.bin ?? CODEX_BIN,
      ["login", "status"],
      { env: childEnv({ auth }, opts.parentEnv), timeout: PROBE_TIMEOUT_MS, encoding: "utf8" },
      (err, stdout, stderr) => {
        const out = `${String(stdout)}\n${String(stderr)}`;
        resolve(err === null && LOGGED_IN_RE.test(out) && !/not logged in/i.test(out));
      },
    );
  });
  return { ok: loggedIn, login_cmd: "codex login" };
}

export const codexAdapter: Adapter = {
  id: "codex",
  bin: CODEX_BIN,
  probeAuth: (auth) => probeAuth(auth),
  run: (req, onEvent) => startCodex(req, onEvent).done,
  effortMap,
};
