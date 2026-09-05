// Structured-output schemas for the five model phases of the unit pipelines (M4.2, E9). The
// engine verifies the facts it can (plan file present, commits on the branch) and treats the
// child's numbers as its report, so every schema stays small and flat.

import type { JsonSchema } from "../../types.ts";

export const MODEL_PHASES = ["plan", "implement", "review", "fix", "watch"] as const;
export type ModelPhase = (typeof MODEL_PHASES)[number];
export const isModelPhase = (p: string): p is ModelPhase =>
  (MODEL_PHASES as readonly string[]).includes(p);

/** `state.resolved` key for a unit phase: `<step id>.<phase>`. */
export function phaseKey(stepId: string, phase: ModelPhase): string {
  return `${stepId}.${phase}`;
}

export const PLAN_STATUSES = ["ready", "insufficient-context", "no-access"] as const;
export type PlanStatus = (typeof PLAN_STATUSES)[number];
export type PlanOutput = { plan_path: string; status: PlanStatus; blueprint_path?: string };

export type ImplementOutput = {
  waves: number;
  tasks: number;
  done: number;
  failed: number;
  commits: number;
};

export const REVIEW_VERDICTS = ["approve", "changes-requested"] as const;
export type ReviewVerdict = (typeof REVIEW_VERDICTS)[number];
export type ReviewOutput = { findings: number; blocking: number; verdict: ReviewVerdict };

export type FixOutput = { fixed: number; skipped: number; commits: number };

export const CI_STATES = ["green", "red", "pending"] as const;
export const BOT_REVIEW_STATES = ["resolved", "open", "stuck", "pending"] as const;
export const WATCH_VERDICTS = ["ready", "wait", "fix", "blocked", "needs-human"] as const;
export type WatchOutput = {
  ci: (typeof CI_STATES)[number];
  bot_reviews: (typeof BOT_REVIEW_STATES)[number];
  human_comment: boolean;
  merged: boolean;
  verdict: (typeof WATCH_VERDICTS)[number];
};

const int = { type: "integer", minimum: 0 } as const;
const bool = { type: "boolean" } as const;
const str = { type: "string" } as const;
const oneOf = (values: readonly string[]) => ({ type: "string", enum: [...values] });

const object = (properties: Record<string, unknown>, required: string[]): JsonSchema => ({
  type: "object",
  properties,
  required,
  additionalProperties: false,
});

export const PHASE_SCHEMAS: Readonly<Record<ModelPhase, JsonSchema>> = {
  plan: object({ plan_path: str, status: oneOf(PLAN_STATUSES), blueprint_path: str }, [
    "plan_path",
    "status",
  ]),
  implement: object({ waves: int, tasks: int, done: int, failed: int, commits: int }, [
    "waves",
    "tasks",
    "done",
    "failed",
    "commits",
  ]),
  review: object({ findings: int, blocking: int, verdict: oneOf(REVIEW_VERDICTS) }, [
    "findings",
    "blocking",
    "verdict",
  ]),
  fix: object({ fixed: int, skipped: int, commits: int }, ["fixed", "skipped", "commits"]),
  watch: object(
    {
      ci: oneOf(CI_STATES),
      bot_reviews: oneOf(BOT_REVIEW_STATES),
      human_comment: bool,
      merged: bool,
      verdict: oneOf(WATCH_VERDICTS),
    },
    ["ci", "bot_reviews", "human_comment", "merged", "verdict"],
  ),
};

// ---- parsers: the child's JSON to a typed record, `undefined` when the shape is off ----------

type Rec = Record<string, unknown>;
const isRec = (v: unknown): v is Rec => typeof v === "object" && v !== null && !Array.isArray(v);
const isInt = (v: unknown): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0;
const inSet = <T extends string>(values: readonly T[], v: unknown): v is T =>
  typeof v === "string" && (values as readonly string[]).includes(v);

export function parsePlan(json: unknown): PlanOutput | undefined {
  if (!isRec(json) || typeof json.plan_path !== "string" || !inSet(PLAN_STATUSES, json.status))
    return undefined;
  const out: PlanOutput = { plan_path: json.plan_path, status: json.status };
  if (typeof json.blueprint_path === "string" && json.blueprint_path.length > 0)
    out.blueprint_path = json.blueprint_path;
  return out;
}

export function parseImplement(json: unknown): ImplementOutput | undefined {
  if (!isRec(json)) return undefined;
  const { waves, tasks, done, failed, commits } = json;
  if (!isInt(waves) || !isInt(tasks) || !isInt(done) || !isInt(failed) || !isInt(commits))
    return undefined;
  return { waves, tasks, done, failed, commits };
}

export function parseReview(json: unknown): ReviewOutput | undefined {
  if (!isRec(json)) return undefined;
  const { findings, blocking, verdict } = json;
  if (!isInt(findings) || !isInt(blocking) || !inSet(REVIEW_VERDICTS, verdict)) return undefined;
  return { findings, blocking, verdict };
}

export function parseFix(json: unknown): FixOutput | undefined {
  if (!isRec(json)) return undefined;
  const { fixed, skipped, commits } = json;
  if (!isInt(fixed) || !isInt(skipped) || !isInt(commits)) return undefined;
  return { fixed, skipped, commits };
}

export function parseWatch(json: unknown): WatchOutput | undefined {
  if (!isRec(json)) return undefined;
  const { ci, bot_reviews, human_comment, merged, verdict } = json;
  if (
    !inSet(CI_STATES, ci) ||
    !inSet(BOT_REVIEW_STATES, bot_reviews) ||
    typeof human_comment !== "boolean" ||
    typeof merged !== "boolean" ||
    !inSet(WATCH_VERDICTS, verdict)
  )
    return undefined;
  return { ci, bot_reviews, human_comment, merged, verdict };
}
