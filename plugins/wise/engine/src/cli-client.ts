// Terminal client (M2.5): `wise-engine run|status|answer|cancel|resume|report|wait` over the
// daemon socket. Follows the same loop a conductor does (P7): preflight -> run -> wait/answer.
// JSON by default, `--text` for a human rendering. Wired into cli.ts by the owner.

import { createInterface } from "node:readline";
import type { Interface } from "node:readline";
import { connect, ConnectError, ensureDaemon } from "./client.ts";
import type { Client, ClientOptions } from "./client.ts";
import { WAIT_DEFAULT_MS, WAIT_MAX_MS } from "./protocol.ts";
import type { ReportResult, RunParams, StatusResult, WaitResult } from "./protocol.ts";
import { domainCode, RpcError } from "./rpc.ts";
import { PROFILE_LEVELS } from "./types.ts";
import type { Answers, Context, Event, Gate, Question, RunSummary } from "./types.ts";

export const CLIENT_USAGE = `wise-engine <command> [options]

Commands:
  run <workflow> [--cwd <dir>] [--answers <json>] [--context <json>] [--input name=value ...]
                 [--profile low|medium|max] [--follow] [--timeout-ms <n>]
                              preflight, fill answers, start a run; --follow streams events and
                              answers gates from stdin (approve|reject, an option value, or text)
  wait <run_id> [--after <seq>] [--timeout-ms <n>]
                              one wait call: events past <seq>, gate, status, done
  status [run_id]             one run or every run
  answer <run_id> <gate_id> <value>
  cancel <run_id> [--reason <text>]
  resume <run_id>
  report <run_id>

Options: --json (default) | --text   --data-root <dir>   --socket <path>   --no-start
Exit codes: 0 ok, 1 error or run failed/cancelled, 2 not found, 64 usage, 69 daemon unavailable
`;

export const CLIENT_COMMANDS = [
  "run",
  "status",
  "answer",
  "cancel",
  "resume",
  "report",
  "wait",
] as const;
export type ClientCommand = (typeof CLIENT_COMMANDS)[number];

export type ClientIo = {
  out: (s: string) => void;
  err: (s: string) => void;
  env?: NodeJS.ProcessEnv;
  stdin?: NodeJS.ReadableStream;
};

// ---- argv ----------------------------------------------------------------------------------

/** Flags that never take a value, so `run --follow wf` keeps `wf` positional. */
const BOOLEAN_FLAGS: ReadonlySet<string> = new Set(["text", "json", "follow", "no-start"]);

type Parsed = { cmd: string; positional: string[]; flags: Record<string, (string | true)[]> };

function parseArgs(argv: readonly string[]): Parsed {
  const [cmd = "help", ...rest] = argv;
  const positional: string[] = [];
  const flags: Record<string, (string | true)[]> = {};
  const add = (name: string, value: string | true): void => {
    (flags[name] ??= []).push(value);
  };
  for (let i = 0; i < rest.length; i++) {
    const tok = rest[i] as string;
    if (!tok.startsWith("--")) {
      positional.push(tok);
      continue;
    }
    const eq = tok.indexOf("=");
    if (eq > 0) {
      add(tok.slice(2, eq), tok.slice(eq + 1));
      continue;
    }
    const name = tok.slice(2);
    const next = rest[i + 1];
    if (!BOOLEAN_FLAGS.has(name) && next !== undefined && !next.startsWith("--")) {
      add(name, next);
      i++;
    } else {
      add(name, true);
    }
  }
  return { cmd, positional, flags };
}

/** Last string value of a flag, or undefined. */
function str(p: Parsed, name: string): string | undefined {
  const values = p.flags[name];
  if (!values) return undefined;
  for (let i = values.length - 1; i >= 0; i--) {
    const v = values[i];
    if (typeof v === "string") return v;
  }
  return undefined;
}

function bool(p: Parsed, name: string): boolean {
  return (p.flags[name] ?? []).some((v) => v === true);
}

function strings(p: Parsed, name: string): string[] {
  return (p.flags[name] ?? []).filter((v): v is string => typeof v === "string");
}

class UsageError extends Error {}

function intFlag(p: Parsed, name: string): number | undefined {
  const raw = str(p, name);
  if (raw === undefined) return undefined;
  if (!/^\d+$/.test(raw)) throw new UsageError(`--${name} must be a non-negative integer`);
  return Number(raw);
}

function jsonFlag<T>(p: Parsed, name: string): T | undefined {
  const raw = str(p, name);
  if (raw === undefined) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    throw new UsageError(`--${name} is not JSON: ${(e as Error).message}`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new UsageError(`--${name} must be a JSON object`);
  }
  return parsed as T;
}

// ---- output ------------------------------------------------------------------------------

type Out = {
  text: boolean;
  /** One result: pretty JSON, or the text rendering. */
  emit: (data: unknown, text: () => string) => void;
  /** One streamed record: NDJSON, or one text line. */
  line: (data: unknown, text: () => string) => void;
  /** An error: JSON on stdout, text on stderr. */
  error: (data: Record<string, unknown>, text: () => string) => void;
};

function makeOut(io: ClientIo, text: boolean): Out {
  return {
    text,
    emit: (data, render) => io.out(text ? render() + "\n" : JSON.stringify(data, null, 2) + "\n"),
    line: (data, render) => io.out(text ? render() + "\n" : JSON.stringify(data) + "\n"),
    error: (data, render) =>
      text ? io.err(render() + "\n") : io.out(JSON.stringify({ error: data }) + "\n"),
  };
}

function clock(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts.slice(11, 19).padEnd(8);
  return d.toISOString().slice(11, 19);
}

function tokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function formatEvent(ev: Event): string {
  const subject = ev.step ?? ev.unit ?? "";
  let detail = ev.verdict ?? ev.message ?? "";
  if (!detail && ev.phase) detail = `phase ${ev.phase}`;
  if (!detail && ev.usage) detail = `in ${tokens(ev.usage.input)} out ${tokens(ev.usage.output)}`;
  if (!detail && ev.model) detail = [ev.harness, ev.model, ev.effort].filter(Boolean).join(" ");
  return `${clock(ev.ts)}  ${ev.type.padEnd(12)}  ${subject}${detail ? `  ${detail}` : ""}`.trimEnd();
}

export function formatGate(gate: Gate): string {
  const lines = [`GATE ${gate.gate_id} [${gate.kind}] step ${gate.step}`, `  ${gate.message}`];
  const options = gateOptions(gate);
  if (options.length) lines.push(`  options: ${options.join(", ")}`);
  if (gate.kind === "ask" && gate.allow_text) lines.push("  free text accepted");
  if (gate.expires_at) lines.push(`  expires ${gate.expires_at}`);
  return lines.join("\n");
}

function gateOptions(gate: Gate): string[] {
  if (gate.options?.length) return gate.options.map((o) => o.value);
  return gate.kind === "approval" ? ["approve", "reject"] : [];
}

function formatSummary(s: RunSummary): string {
  const gate = s.gate ? `  gate ${s.gate.gate_id}` : "";
  return `${s.run_id}  ${s.workflow}  ${s.status}  ${s.started_at}  ${s.cwd}${gate}`;
}

function formatStatus(res: StatusResult): string {
  if (Array.isArray(res)) return res.length ? res.map(formatSummary).join("\n") : "no runs";
  return formatSummary(res);
}

function formatWait(res: WaitResult): string {
  const lines = res.events.map(formatEvent);
  if (res.gate) lines.push(formatGate(res.gate));
  lines.push(`status: ${res.status}${res.done ? "  done" : ""}`);
  return lines.join("\n");
}

function formatReport(res: ReportResult): string {
  const lines: string[] = [];
  const verdicts = Object.entries(res.verdicts);
  lines.push(verdicts.length ? "verdicts:" : "verdicts: none");
  for (const [step, verdict] of verdicts) lines.push(`  ${step}: ${verdict}`);
  lines.push(res.units.length ? `units (${res.units.length}):` : "units: none");
  for (const u of res.units) {
    const reason = u.reason ? `  ${u.reason}` : "";
    lines.push(`  ${u.unit.ref}  ${u.verdict ?? "-"}  ${u.unit.branch}${reason}`);
  }
  lines.push("usage:");
  for (const pool of ["subscription", "api-key"] as const) {
    const u = res.usage[pool];
    if (!u) continue;
    const cost = u.cost_usd !== undefined ? `  $${u.cost_usd.toFixed(2)}` : "";
    lines.push(`  ${pool}: in ${tokens(u.input)} out ${tokens(u.output)}${cost}`);
  }
  return lines.join("\n");
}

// ---- stdin ------------------------------------------------------------------------------------------

/** Lazy line reader: never touches the stream until a gate needs an answer. */
class LineSource {
  private readonly input: NodeJS.ReadableStream;
  private rl: Interface | undefined;
  private readonly lines: string[] = [];
  private readonly waiters: ((line: string | null) => void)[] = [];
  private ended = false;

  constructor(input: NodeJS.ReadableStream) {
    this.input = input;
  }

  next(): Promise<string | null> {
    if (!this.rl) this.start();
    const queued = this.lines.shift();
    if (queued !== undefined) return Promise.resolve(queued);
    if (this.ended) return Promise.resolve(null);
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  close(): void {
    this.rl?.close();
  }

  private start(): void {
    this.rl = createInterface({ input: this.input, terminal: false });
    this.rl.on("line", (line: string) => {
      const waiter = this.waiters.shift();
      if (waiter) waiter(line);
      else this.lines.push(line);
    });
    this.rl.on("close", () => {
      this.ended = true;
      for (const w of this.waiters.splice(0)) w(null);
    });
  }
}

// ---- answers ---------------------------------------------------------------------------------------

export type FilledAnswers = { answers: Answers; inputs: Record<string, string>; missing: string[] };

/**
 * Explicit answers win; every unanswered, non-locked question falls back to its default. A
 * non-optional question left without a value is reported in `missing`.
 */
export function fillAnswers(questions: Question[], given: Answers): FilledAnswers {
  const answers: Answers = { ...given };
  const missing: string[] = [];
  for (const q of questions) {
    if (q.locked) continue;
    if (answers[q.id] !== undefined) continue;
    if (q.default !== undefined) {
      answers[q.id] = q.default;
      continue;
    }
    if (q.kind === "text" && q.optional) continue;
    missing.push(q.id);
  }
  const inputs: Record<string, string> = {};
  for (const [id, value] of Object.entries(answers)) {
    if (id.startsWith("input.") && typeof value === "string") inputs[id.slice(6)] = value;
  }
  return { answers, inputs, missing };
}

// ---- connection --------------------------------------------------------------------------------

function clientOptions(p: Parsed, io: ClientIo): ClientOptions {
  const opts: ClientOptions = { env: io.env ?? process.env, client: "wise-engine-cli" };
  const dataRoot = str(p, "data-root");
  const socket = str(p, "socket");
  if (dataRoot) opts.dataRoot = dataRoot;
  if (socket) opts.socketPath = socket;
  return opts;
}

async function open(p: Parsed, io: ClientIo): Promise<Client> {
  const opts = clientOptions(p, io);
  return bool(p, "no-start") ? connect(opts) : ensureDaemon(opts);
}

function requireArg(p: Parsed, index: number, name: string): string {
  const v = p.positional[index];
  if (!v) throw new UsageError(`${p.cmd}: missing <${name}>`);
  return v;
}

// ---- follow loop ------------------------------------------------------------------------------

type Follow = { client: Client; runId: string; timeoutMs: number; out: Out; stdin: LineSource };

/** Read a gate answer from stdin; `null` when stdin closed before a usable line arrived. */
async function readGateAnswer(gate: Gate, f: Follow): Promise<string | null> {
  const options = gateOptions(gate);
  const freeText = gate.kind === "ask" && (gate.allow_text === true || options.length === 0);
  for (;;) {
    if (f.out.text) f.out.line(null, () => "> ");
    const raw = await f.stdin.next();
    if (raw === null) return null;
    const value = raw.trim();
    if (value === "") continue;
    if (freeText || options.includes(value)) return value;
    f.out.line(
      { warn: "invalid answer", value, options },
      () => `expected one of: ${options.join(", ")}`,
    );
  }
}

async function follow(f: Follow): Promise<number> {
  let after = 0;
  const answered = new Set<string>();
  for (;;) {
    const res = await f.client.call(
      "wait",
      { run_id: f.runId, after, timeout_ms: f.timeoutMs },
      { timeoutMs: f.timeoutMs + 15_000 },
    );
    for (const ev of res.events) {
      f.out.line(ev, () => formatEvent(ev));
      if (ev.seq > after) after = ev.seq;
    }
    if (res.done) {
      f.out.line({ done: true, run_id: f.runId, status: res.status }, () => `run ${res.status}`);
      return res.status === "completed" ? 0 : 1;
    }
    if (res.gate && !answered.has(res.gate.gate_id)) {
      const gate = res.gate;
      f.out.line({ gate }, () => formatGate(gate));
      const value = await readGateAnswer(gate, f);
      if (value === null) {
        f.out.error(
          { code: "GATE_UNANSWERED", run_id: f.runId, gate_id: gate.gate_id },
          () =>
            `stdin closed before gate ${gate.gate_id} was answered; run stays gated.\n` +
            `answer later with: wise-engine answer ${f.runId} ${gate.gate_id} <value>`,
        );
        return 1;
      }
      const ack = await f.client.call("answer", {
        run_id: f.runId,
        gate_id: gate.gate_id,
        value,
      });
      answered.add(gate.gate_id);
      f.out.line({ answered: gate.gate_id, value, accepted: ack.accepted }, () =>
        ack.accepted
          ? `answered ${gate.gate_id}: ${value}`
          : `answer to ${gate.gate_id} not accepted`,
      );
    }
  }
}

// ---- commands --------------------------------------------------------------------------------------

async function cmdRun(p: Parsed, io: ClientIo, out: Out): Promise<number> {
  const workflow = requireArg(p, 0, "workflow");
  const cwd = str(p, "cwd") ?? process.cwd();
  const profile = str(p, "profile");
  if (profile !== undefined && !(PROFILE_LEVELS as readonly string[]).includes(profile)) {
    throw new UsageError(`--profile must be one of ${PROFILE_LEVELS.join("|")}`);
  }
  const given: Answers = { ...jsonFlag<Answers>(p, "answers") };
  const context: Context = jsonFlag<Context>(p, "context") ?? {};
  if (profile !== undefined) given.profile = profile;
  for (const pair of strings(p, "input")) {
    const eq = pair.indexOf("=");
    if (eq <= 0) throw new UsageError(`--input expects name=value, got '${pair}'`);
    given[`input.${pair.slice(0, eq)}`] = pair.slice(eq + 1);
  }
  const timeoutMs = Math.min(intFlag(p, "timeout-ms") ?? WAIT_DEFAULT_MS, WAIT_MAX_MS);

  const client = await open(p, io);
  const stdin = new LineSource(io.stdin ?? process.stdin);
  try {
    const pre = await client.call("preflight", { workflow, cwd });
    const filled = fillAnswers(pre.questions, given);
    if (filled.missing.length) {
      const questions = pre.questions.filter((q) => filled.missing.includes(q.id));
      out.error(
        { code: "MISSING_ANSWERS", workflow: pre.workflow, missing: filled.missing, questions },
        () =>
          [
            `run: missing required answers (no interactive prompting in this version): ${filled.missing.join(", ")}`,
            ...questions.map((q) => `  ${q.id} [${q.kind}] ${q.label}`),
            `  pass them with --input <name>=<value> or --answers '{"<id>":"<value>"}'`,
          ].join("\n"),
      );
      return 64;
    }
    const params: RunParams = {
      workflow,
      cwd,
      answers: filled.answers,
      context,
      inputs: filled.inputs,
    };
    const started = await client.call("run", params);
    const record = { ...started, workflow: pre.workflow, answers: filled.answers };
    if (bool(p, "follow")) {
      out.line(record, () => `run ${started.run_id} started (${pre.workflow})`);
      return await follow({ client, runId: started.run_id, timeoutMs, out, stdin });
    }
    out.emit(record, () => `run ${started.run_id} started (${pre.workflow})`);
    return 0;
  } finally {
    stdin.close();
    client.close();
  }
}

async function cmdWait(p: Parsed, io: ClientIo, out: Out): Promise<number> {
  const runId = requireArg(p, 0, "run_id");
  const after = intFlag(p, "after");
  const timeoutMs = Math.min(intFlag(p, "timeout-ms") ?? WAIT_DEFAULT_MS, WAIT_MAX_MS);
  const params: { run_id: string; after?: number; timeout_ms: number } = {
    run_id: runId,
    timeout_ms: timeoutMs,
  };
  if (after !== undefined) params.after = after;
  const client = await open(p, io);
  try {
    const res = await client.call("wait", params, { timeoutMs: timeoutMs + 15_000 });
    out.emit(res, () => formatWait(res));
    return 0;
  } finally {
    client.close();
  }
}

async function cmdDirect(p: Parsed, io: ClientIo, out: Out): Promise<number> {
  const client = await open(p, io);
  try {
    switch (p.cmd) {
      case "status": {
        const runId = p.positional[0];
        const res = await client.call("status", runId ? { run_id: runId } : {});
        out.emit(res, () => formatStatus(res));
        return 0;
      }
      case "answer": {
        const params = {
          run_id: requireArg(p, 0, "run_id"),
          gate_id: requireArg(p, 1, "gate_id"),
          value: requireArg(p, 2, "value"),
        };
        const res = await client.call("answer", params);
        out.emit(res, () =>
          res.accepted ? `answer accepted for ${params.gate_id}` : `answer not accepted`,
        );
        return res.accepted ? 0 : 1;
      }
      case "cancel": {
        const params: { run_id: string; reason?: string } = { run_id: requireArg(p, 0, "run_id") };
        const reason = str(p, "reason");
        if (reason !== undefined) params.reason = reason;
        const res = await client.call("cancel", params);
        out.emit(res, () => `run ${params.run_id} ${res.status}`);
        return 0;
      }
      case "resume": {
        const res = await client.call("resume", { run_id: requireArg(p, 0, "run_id") });
        out.emit(res, () => `run ${res.run_id} ${res.status}`);
        return 0;
      }
      case "report": {
        const res = await client.call("report", { run_id: requireArg(p, 0, "run_id") });
        out.emit(res, () => formatReport(res));
        return 0;
      }
      default:
        throw new UsageError(`unknown command '${p.cmd}'`);
    }
  } finally {
    client.close();
  }
}

// ---- entry -----------------------------------------------------------------------------------------

const NOT_FOUND: ReadonlySet<string> = new Set(["WORKFLOW_NOT_FOUND", "RUN_NOT_FOUND"]);

function reportError(err: unknown, out: Out, io: ClientIo): number {
  if (err instanceof UsageError) {
    io.err(`wise-engine ${err.message}\n\n${CLIENT_USAGE}`);
    return 64;
  }
  if (err instanceof ConnectError) {
    out.error(
      { code: err.code, message: err.message, hint: "run /wise-init" },
      () => `ERROR ${err.code}: ${err.message}\nrun /wise-init to set up the engine and its daemon`,
    );
    return 69;
  }
  const code = domainCode(err);
  if (code !== undefined && err instanceof RpcError) {
    const data = (err.data ?? {}) as Record<string, unknown>;
    out.error({ ...data, code, message: err.message }, () => {
      const lines = [`ERROR ${code}: ${err.message}`];
      if (code === "AUTH_REQUIRED" && typeof data.login_cmd === "string") {
        lines.push(data.login_cmd);
      }
      return lines.join("\n");
    });
    return NOT_FOUND.has(code) ? 2 : 1;
  }
  if (err instanceof RpcError) {
    out.error(
      { code: "RPC_ERROR", rpc_code: err.code, message: err.message },
      () => `ERROR RPC_ERROR (${err.code}): ${err.message}`,
    );
    return 70;
  }
  const message = err instanceof Error ? err.message : String(err);
  out.error({ code: "INTERNAL", message }, () => `ERROR INTERNAL: ${message}`);
  return 70;
}

/** `argv[0]` is the command (`run`, `status`, ...); the rest are its arguments. */
export async function clientCommand(argv: readonly string[], io: ClientIo): Promise<number> {
  const p = parseArgs(argv);
  const out = makeOut(io, bool(p, "text"));
  try {
    switch (p.cmd) {
      case "run":
        return await cmdRun(p, io, out);
      case "wait":
        return await cmdWait(p, io, out);
      case "status":
      case "answer":
      case "cancel":
      case "resume":
      case "report":
        return await cmdDirect(p, io, out);
      case "help":
      case "--help":
      case "-h":
        io.out(CLIENT_USAGE);
        return 0;
      default:
        throw new UsageError(`unknown command '${p.cmd}'`);
    }
  } catch (err) {
    return reportError(err, out, io);
  }
}
