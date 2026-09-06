// DAG scheduler: wave computation from `depends_on`, trigger rules, `when:` gating,
// skip propagation and run-completion detection. Port of `_step_by_id`,
// `_trigger_rule_satisfied` and `cmd_next_wave` from scripts/workflows.py, plus the
// v2 `when:` expression evaluator. Pure: no I/O.

import type { State, StepState, Step, WorkflowDef } from "./types.ts";
import { TERMINAL_STEP, TRIGGER_RULES } from "./types.ts";

// ---- trigger rules ----------------------------------------------------------

export type TriggerVerdict = { runnable: boolean; skip: boolean };

type StatusOnly = Pick<StepState, "status">;

export function stepById(steps: readonly Step[], id: string): Step | undefined {
  return steps.find((s) => s.id === id);
}

/** Return whether the step may run now and whether it must be skipped instead. */
export function triggerRuleSatisfied(rule: string, deps: readonly StatusOnly[]): TriggerVerdict {
  if (deps.length === 0) return { runnable: true, skip: false };
  const statuses = deps.map((d) => d.status);
  const terminal = statuses.filter((s) => TERMINAL_STEP.has(s)).length;
  const completed = statuses.filter((s) => s === "completed").length;
  const failed = statuses.filter((s) => s === "failed").length;
  const allTerminal = terminal === statuses.length;

  switch (rule) {
    case "all-success":
      if (completed === statuses.length) return { runnable: true, skip: false };
      if (statuses.some((s) => s === "failed" || s === "skipped" || s === "cancelled")) {
        return { runnable: false, skip: true };
      }
      return { runnable: false, skip: false };
    case "one-success":
      if (completed > 0) return { runnable: true, skip: false };
      if (allTerminal) return { runnable: false, skip: true };
      return { runnable: false, skip: false };
    case "all-done":
      return { runnable: allTerminal, skip: false };
    case "none-failed":
      // Tolerates ALL deps skipped (user-deselected stages); a failed dep
      // propagates the skip before the rest finish.
      if (failed > 0) return { runnable: false, skip: true };
      return { runnable: allTerminal, skip: false };
    case "none-failed-min-one-success":
      if (allTerminal && failed === 0 && completed > 0) return { runnable: true, skip: false };
      if (failed > 0) return { runnable: false, skip: true };
      return { runnable: false, skip: false };
    default:
      // Unknown rule behaves like all-success.
      return triggerRuleSatisfied("all-success", deps);
  }
}

export function isTriggerRule(rule: string): boolean {
  return (TRIGGER_RULES as readonly string[]).includes(rule);
}

// ---- `when:` expression evaluator ---------------------------------------------
//
// Grammar (lowest to highest precedence):
//   or     := and ('||' and)*
//   and    := eq ('&&' eq)*
//   eq     := unary (('==' | '!=') unary)*
//   unary  := '!' unary | primary
//   primary := '(' or ')' | string | number | true | false | identifier
// Identifiers may contain dots. Unset identifiers evaluate to `undefined`.

type TokKind = "ident" | "string" | "number" | "op" | "lparen" | "rparen" | "end";
type Token = { kind: TokKind; value: string; pos: number };

function whenError(expr: string, message: string, pos: number): Error {
  return new Error(`when: ${message} at position ${pos} in ${JSON.stringify(expr)}`);
}

function isIdentStart(ch: string): boolean {
  return /[A-Za-z_]/.test(ch);
}

function isIdentPart(ch: string): boolean {
  return /[A-Za-z0-9_]/.test(ch);
}

function tokenize(expr: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  while (i < expr.length) {
    const ch = expr[i] as string;
    if (/\s/.test(ch)) {
      i += 1;
      continue;
    }
    const start = i;
    if (ch === "(") {
      tokens.push({ kind: "lparen", value: ch, pos: start });
      i += 1;
    } else if (ch === ")") {
      tokens.push({ kind: "rparen", value: ch, pos: start });
      i += 1;
    } else if (ch === "'" || ch === '"') {
      let j = i + 1;
      while (j < expr.length && expr[j] !== ch) j += 1;
      if (j >= expr.length) throw whenError(expr, "unterminated string", start);
      tokens.push({ kind: "string", value: expr.slice(i + 1, j), pos: start });
      i = j + 1;
    } else if (/[0-9]/.test(ch)) {
      let j = i;
      while (j < expr.length && /[0-9.]/.test(expr[j] as string)) j += 1;
      const text = expr.slice(i, j);
      if (!/^\d+(\.\d+)?$/.test(text)) throw whenError(expr, `bad number ${text}`, start);
      tokens.push({ kind: "number", value: text, pos: start });
      i = j;
    } else if (isIdentStart(ch)) {
      let j = i;
      while (j < expr.length && (isIdentPart(expr[j] as string) || expr[j] === ".")) j += 1;
      const text = expr.slice(i, j);
      if (!/^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$/.test(text)) {
        throw whenError(expr, `bad identifier ${text}`, start);
      }
      tokens.push({ kind: "ident", value: text, pos: start });
      i = j;
    } else {
      const two = expr.slice(i, i + 2);
      if (two === "==" || two === "!=" || two === "&&" || two === "||") {
        tokens.push({ kind: "op", value: two, pos: start });
        i += 2;
      } else if (ch === "!") {
        tokens.push({ kind: "op", value: ch, pos: start });
        i += 1;
      } else {
        throw whenError(expr, `unexpected character ${JSON.stringify(ch)}`, start);
      }
    }
  }
  tokens.push({ kind: "end", value: "", pos: expr.length });
  return tokens;
}

/** Bare identifier truthiness: non-empty string, non-zero number, true, non-empty array. */
export function truthy(value: unknown): boolean {
  if (value === undefined || value === null) return false;
  if (typeof value === "string") return value !== "";
  if (typeof value === "number") return value !== 0 && !Number.isNaN(value);
  if (typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.length > 0;
  return true;
}

/** Unset never equals a set value; numbers and booleans compare to strings by their text. */
function valuesEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (a === undefined || b === undefined || a === null || b === null) return false;
  if (typeof a === "object" || typeof b === "object") return false;
  return String(a) === String(b);
}

function walk(root: unknown, path: readonly string[]): unknown {
  let cur: unknown = root;
  for (const seg of path) {
    if (cur === null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[seg];
  }
  return cur;
}

const BARE_ROOTS = ["outputs", "inputs", "answers"] as const;

/** Dotted names walk from the scope root; unknown roots fall back to outputs, inputs, answers. */
export function resolveIdentifier(name: string, scope: Record<string, unknown>): unknown {
  const path = name.split(".");
  const head = path[0] as string;
  if (Object.hasOwn(scope, head)) return walk(scope, path);
  for (const root of BARE_ROOTS) {
    const bucket = scope[root];
    if (bucket !== null && typeof bucket === "object" && Object.hasOwn(bucket, head)) {
      return walk(bucket, path);
    }
  }
  return undefined;
}

export function evaluateWhen(expr: string, scope: Record<string, unknown>): boolean {
  const tokens = tokenize(expr);
  let idx = 0;
  const peek = (): Token => tokens[idx] as Token;
  const next = (): Token => tokens[idx++] as Token;
  const isOp = (value: string): boolean => peek().kind === "op" && peek().value === value;

  const parsePrimary = (): unknown => {
    const tok = next();
    switch (tok.kind) {
      case "lparen": {
        const value = parseOr();
        if (peek().kind !== "rparen") throw whenError(expr, "expected ')'", peek().pos);
        idx += 1;
        return value;
      }
      case "string":
        return tok.value;
      case "number":
        return Number(tok.value);
      case "ident":
        if (tok.value === "true") return true;
        if (tok.value === "false") return false;
        return resolveIdentifier(tok.value, scope);
      case "end":
        throw whenError(expr, "unexpected end of expression", tok.pos);
      default:
        throw whenError(expr, `unexpected ${JSON.stringify(tok.value)}`, tok.pos);
    }
  };

  const parseUnary = (): unknown => {
    if (isOp("!")) {
      idx += 1;
      return !truthy(parseUnary());
    }
    return parsePrimary();
  };

  const parseEq = (): unknown => {
    let left = parseUnary();
    while (isOp("==") || isOp("!=")) {
      const op = next().value;
      const right = parseUnary();
      const eq = valuesEqual(left, right);
      left = op === "==" ? eq : !eq;
    }
    return left;
  };

  const parseAnd = (): unknown => {
    let left = parseEq();
    while (isOp("&&")) {
      idx += 1;
      const right = parseEq();
      left = truthy(left) && truthy(right);
    }
    return left;
  };

  const parseOr = (): unknown => {
    let left = parseAnd();
    while (isOp("||")) {
      idx += 1;
      const right = parseAnd();
      left = truthy(left) || truthy(right);
    }
    return left;
  };

  const result = parseOr();
  const tail = peek();
  if (tail.kind !== "end") {
    throw whenError(expr, `unexpected ${JSON.stringify(tail.value)}`, tail.pos);
  }
  return truthy(result);
}

// ---- next wave ----------------------------------------------------------------

export type SkipReason = { id: string; reason: string };
export type Wave = {
  ready: Step[];
  skipped: SkipReason[];
  /** No step can run or be skipped and nothing is in flight. */
  done: boolean;
  /** Set with `done` when a step failed or a pending step is unreachable. */
  failed: boolean;
  /** Non-fatal notices, e.g. an unparseable `when:` treated as true. */
  warnings: string[];
};

/** The scope `when:` expressions evaluate against. */
export function whenScope(state: State): Record<string, unknown> {
  return { outputs: state.outputs, inputs: state.inputs, answers: state.answers };
}

/** `when:` is a string in v2; a v1 list of conditions is still accepted and AND-ed. */
function whenConditions(when: unknown): string[] {
  if (Array.isArray(when)) return when.map((c) => String(c));
  if (when === undefined || when === null || when === "") return [];
  return [String(when)];
}

type WhenGate = { ok: true } | { ok: false; condition: string };

function evaluateGate(
  stepId: string,
  conditions: readonly string[],
  scope: Record<string, unknown>,
  warnings: string[],
): WhenGate {
  for (const condition of conditions) {
    let holds: boolean;
    try {
      holds = evaluateWhen(condition, scope);
    } catch (err) {
      // Never block a step on a typo: an unparseable condition is treated as
      // true but surfaced so the caller can report it.
      const message = err instanceof Error ? err.message : String(err);
      warnings.push(`when-unparseable:${stepId}:${message}`);
      continue;
    }
    if (!holds) return { ok: false, condition };
  }
  return { ok: true };
}

export function nextWave(def: WorkflowDef, state: State): Wave {
  const ready: Step[] = [];
  const skipped: SkipReason[] = [];
  const warnings: string[] = [];
  const scope = whenScope(state);

  for (const sdef of def.steps) {
    const st = state.steps[sdef.id];
    if (!st || st.status !== "pending") continue;
    const depIds = sdef.depends_on ?? [];
    const deps = depIds.flatMap((d) => {
      const dep = state.steps[d];
      return dep ? [{ id: d, status: dep.status }] : [];
    });
    const rule = sdef["trigger-rule"] ?? "all-success";
    const verdict = triggerRuleSatisfied(rule, deps);
    if (verdict.skip) {
      const summary = deps.map((d) => `${d.id}=${d.status}`).join(", ");
      skipped.push({ id: sdef.id, reason: `trigger-rule ${rule} not satisfied: ${summary}` });
      continue;
    }
    if (!verdict.runnable) continue;
    const gate = evaluateGate(sdef.id, whenConditions(sdef.when), scope, warnings);
    if (gate.ok) {
      ready.push(sdef);
    } else {
      skipped.push({ id: sdef.id, reason: `when: ${gate.condition} is false` });
    }
  }

  const statuses = Object.values(state.steps).map((s) => s.status);
  const inFlight = statuses.some((s) => s === "running");
  const anyPending = statuses.some((s) => s === "pending");
  const anyFailed = statuses.some((s) => s === "failed");
  const allDone = statuses.every((s) => TERMINAL_STEP.has(s));

  let done = false;
  let failed = false;
  if (ready.length === 0 && skipped.length === 0 && !inFlight) {
    if (anyFailed) {
      done = true;
      failed = true;
    } else if (allDone) {
      done = true;
    } else if (anyPending) {
      // Pending steps whose dependencies can never resolve: treat as failed.
      done = true;
      failed = true;
    }
  }
  return { ready, skipped, done, failed, warnings };
}
