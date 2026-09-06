// Pre-flight questionary (D11/D12): the engine builds the questions, the harness asks them.
// Per unlocked tuning group the questions come in stages, each unlocked by the answer before it:
// `harness.<group>` (which ready CLI), then `model.<group>` (that harness's catalog), then
// `effort.<group>` (that model's efforts). `step-select` and `input.<name>` are stage-free.
// The conductor calls `preflight` again with the answers so far until no new question appears.
// Pure: no I/O.

import { HARNESSES } from "./types.ts";
import type {
  Answers,
  Context,
  Effort,
  Harness,
  ProfileLevel,
  Question,
  QuestionOption,
  Step,
  TuningDefault,
  TuningGroup,
  WorkflowDef,
} from "./types.ts";
import { PROFILE_DEFAULT } from "./profile.ts";
import { listInputs } from "./defs.ts";
import { catalogFor, catalogModel, defaultEffort, defaultModel } from "./models.ts";
import type { CatalogModel } from "./models.ts";

export type Questionary = { questions: Question[]; defaults: Answers };

export type QuestionaryCtx = {
  context?: Context;
  /**
   * Harnesses ready to run besides a group's default (adapter present, logged in). Any of them
   * puts a `harness.<group>` question on every unlocked group; none leaves the default harness.
   */
  harnesses?: readonly Harness[];
};

const isHarness = (v: string): v is Harness => (HARNESSES as readonly string[]).includes(v);

/** `claude / opus / high` style summary of a tuning value; `inherit` when empty. */
export function describeTuning(value: TuningDefault): string {
  const parts = [value.harness, value.model, value.effort].filter((p): p is string => Boolean(p));
  return parts.length ? parts.join(" / ") : "inherit";
}

function answerString(value: string | string[] | undefined): string | undefined {
  if (value === undefined) return undefined;
  return Array.isArray(value) ? value.join(", ") : value;
}
function answerList(value: string | string[] | undefined): string[] | undefined {
  if (value === undefined) return undefined;
  return Array.isArray(value)
    ? value
    : value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
}

// ---- per-group stages --------------------------------------------------------------------------------

/** The group's declared value with the workflow's `profiles.medium` tuning folded in. */
function groupBase(def: WorkflowDef, group: TuningGroup): TuningDefault {
  return { ...group.default, ...def.profiles?.[PROFILE_DEFAULT]?.tuning?.[group.id] };
}

type Stage = {
  base: TuningDefault;
  /** Set once the harness is known: answered, or no question asked. */
  harness?: Harness;
  /** Set once the model is known. */
  model?: CatalogModel;
  effort?: Effort;
  questions: Question[];
};

/**
 * Walk one unlocked group's stages against the answers so far. Each stage either records its
 * value (answered, or nothing to ask) and moves on, or emits its question and stops.
 */
function stageGroup(
  def: WorkflowDef,
  group: TuningGroup,
  answers: Answers,
  ready: readonly Harness[] | undefined,
): Stage {
  const base = groupBase(def, group);
  const label = group.label ?? group.id;
  const stage: Stage = { base, questions: [] };
  const defaultHarness: Harness = base.harness ?? "claude";

  // The default harness is always offered (the run's auth probe checks it); `ready` adds the rest.
  const offered: Harness[] = [defaultHarness, ...(ready ?? []).filter((h) => h !== defaultHarness)];
  const harnessAnswer = answerString(answers[`harness.${group.id}`]);
  if (harnessAnswer !== undefined && isHarness(harnessAnswer)) {
    stage.harness = harnessAnswer;
  } else if (offered.length > 1) {
    const options: QuestionOption[] = offered.map((h) => ({
      value: h,
      label: h,
      description: h === defaultHarness ? "the workflow's default" : `run these steps on ${h}`,
    }));
    stage.questions.push({
      id: `harness.${group.id}`,
      kind: "choice",
      label: `Which CLI runs: ${label}?`,
      options,
      default: defaultHarness,
    });
    return stage;
  } else {
    stage.harness = defaultHarness;
  }
  const harness = stage.harness;

  // A pin from another harness means nothing here; the catalog's first entry stands in.
  const pinned = harness === defaultHarness ? base.model : undefined;
  const modelAnswer = catalogModel(harness, answerString(answers[`model.${group.id}`]));
  const catalog = catalogFor(harness);
  if (modelAnswer) {
    stage.model = modelAnswer;
  } else if (catalog.length > 1) {
    stage.questions.push({
      id: `model.${group.id}`,
      kind: "choice",
      label: `Which ${harness} model: ${label}?`,
      options: catalog.map((m) => ({ value: m.id, label: m.label, description: m.description })),
      default: defaultModel(harness, pinned).id,
    });
    return stage;
  } else {
    stage.model = defaultModel(harness, pinned);
  }
  const model = stage.model;

  // The effort scale is wise-wide, so the declared effort stands whichever harness was picked.
  const effortAnswer = answerString(answers[`effort.${group.id}`]);
  const wanted = base.effort;
  if (effortAnswer !== undefined && (model.efforts as readonly string[]).includes(effortAnswer)) {
    stage.effort = effortAnswer as Effort;
  } else if (model.efforts.length > 1) {
    stage.questions.push({
      id: `effort.${group.id}`,
      kind: "choice",
      label: `Effort for ${model.label}: ${label}?`,
      options: model.efforts.map((e) => ({ value: e, label: e })),
      default: defaultEffort(model, wanted) as string,
    });
  } else {
    const e = defaultEffort(model, wanted);
    if (e !== undefined) stage.effort = e;
  }
  return stage;
}

// ---- step-select / inputs ----------------------------------------------------------------------------

/** Step ids the user may switch off: `step-select.optional`, else every `optional: true` step. */
export function optionalStepIds(def: WorkflowDef): string[] {
  const declared = def["step-select"]?.optional;
  if (declared !== undefined) return [...declared];
  return def.steps.filter((s) => s.optional === true).map((s) => s.id);
}

function stepSelectQuestion(def: WorkflowDef, optional: readonly string[]): Question {
  const byId = new Map<string, Step>(def.steps.map((s) => [s.id, s]));
  const options: QuestionOption[] = optional.map((id) => {
    const step = byId.get(id);
    const option: QuestionOption = { value: id, label: step?.description ?? id };
    if (step?.description) option.description = id;
    return option;
  });
  return {
    id: "step-select",
    kind: "multi",
    label: def["step-select"]?.prompt ?? "Which optional steps should run?",
    options,
    default: [...optional],
  };
}

/**
 * Resolve a `from-context` path (E1) against the harness-supplied context.
 * `ticket[].<field>` joins every ticket's field with `, `; `links[]` joins with newlines.
 */
export function resolveFromContext(path: string, context: Context | undefined): string | undefined {
  if (!context) return undefined;
  if (path === "guidance") return context.guidance?.trim() || undefined;
  if (path === "links[]") return context.links?.length ? context.links.join("\n") : undefined;
  const ticket = /^ticket\[\]\.(ref|title|body|url)$/.exec(path);
  if (ticket) {
    const field = ticket[1] as "ref" | "title" | "body" | "url";
    const values = (context.ticket ?? [])
      .map((t) => t[field])
      .filter((v): v is string => Boolean(v));
    return values.length ? values.join(", ") : undefined;
  }
  const decision = /^decisions\.(.+)$/.exec(path);
  if (decision) return context.decisions?.[decision[1] as string];
  return undefined;
}

// ---- the questionary ---------------------------------------------------------------------------------

/**
 * Build the questionary for the answers given so far, in order: per unlocked tuning group the
 * next unanswered stage (`harness.<group>` when two or more harnesses are ready, `model.<group>`,
 * `effort.<group>`; a stage with one possible value is skipped), `step-select` when the workflow
 * has optional steps, and `input.<name>` per declared input with `from-context` pre-fill.
 * Locked groups ask nothing. Answered questions are not repeated.
 */
export function buildQuestionary(
  def: WorkflowDef,
  ctx: QuestionaryCtx = {},
  answers: Answers = {},
): Questionary {
  const questions: Question[] = [];
  const defaults: Answers = {};
  const push = (q: Question): void => {
    if (answers[q.id] !== undefined) return;
    questions.push(q);
    if (q.default !== undefined) defaults[q.id] = q.default;
  };

  for (const group of def.tuning?.groups ?? []) {
    if (group.locked) continue;
    for (const q of stageGroup(def, group, answers, ctx.harnesses).questions) push(q);
  }
  const optional = optionalStepIds(def);
  if (optional.length) push(stepSelectQuestion(def, optional));
  for (const input of listInputs(def)) {
    const q: Question = { id: `input.${input.name}`, kind: "text", label: input.prompt };
    const fromContext = input["from-context"]
      ? resolveFromContext(input["from-context"], ctx.context)
      : undefined;
    const preset = fromContext ?? input.default ?? (input.optional ? "" : undefined);
    if (preset !== undefined) q.default = preset;
    push(q);
  }
  return { questions, defaults };
}

export type Applied = {
  /** Always `medium`: the workflow's declared defaults. Budget profiles no longer reach workflows. */
  profile: ProfileLevel;
  /** Effective tuning per group: the staged answers over the group default. */
  tuning: Record<string, TuningDefault>;
  enabledSteps: Set<string>;
  inputs: Record<string, string>;
  caps: Record<string, number>;
};

/**
 * Merge answers into run parameters. Per unlocked group the `harness.<group>` answer (else the
 * group's default harness), the `model.<group>` answer (else the default's catalog entry, else
 * the catalog's first model) and the `effort.<group>` answer (else the model's default for the
 * group's effort) replace the declared value; a model without effort control drops the effort.
 * Locked groups keep their declared value. Steps not selected in `step-select` are disabled;
 * every non-optional step is always enabled. Retired-id and ceiling clamping is `resolve`'s job.
 */
export function applyAnswers(def: WorkflowDef, answers: Answers): Applied {
  const profile: ProfileLevel = PROFILE_DEFAULT;
  const profileDef = def.profiles?.[profile];

  const tuning: Record<string, TuningDefault> = {};
  for (const group of def.tuning?.groups ?? []) {
    if (group.locked) {
      tuning[group.id] = groupBase(def, group);
      continue;
    }
    const stage = stageGroup(def, group, answers, undefined);
    const harness = stage.harness ?? stage.base.harness ?? "claude";
    const model =
      stage.model ??
      defaultModel(
        harness,
        harness === (stage.base.harness ?? "claude") ? stage.base.model : undefined,
      );
    const effort = stage.effort ?? defaultEffort(model, stage.base.effort);
    const value: TuningDefault = { ...stage.base, harness, model: model.id };
    if (effort !== undefined) value.effort = effort;
    else delete value.effort;
    tuning[group.id] = value;
  }

  const optional = new Set(optionalStepIds(def));
  const selected = answerList(answers["step-select"]);
  const enabledSteps = new Set<string>();
  for (const step of def.steps) {
    if (!optional.has(step.id) || selected === undefined || selected.includes(step.id))
      enabledSteps.add(step.id);
  }

  const inputs: Record<string, string> = {};
  for (const input of def.inputs ?? []) {
    const value = answerString(answers[`input.${input.name}`]) ?? input.default;
    if (value !== undefined) inputs[input.name] = value;
  }

  return { profile, tuning, enabledSteps, inputs, caps: { ...profileDef?.caps } };
}

// ---- answers -----------------------------------------------------------------------------------

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

export type CompletedAnswers = FilledAnswers & {
  /** Every question the staged walk produced, in the order it appeared. */
  questions: Question[];
};

/**
 * Run the staged questionary to the end without a user: build, fill defaults, rebuild with the
 * new answers, until no question is left unanswered. What `wise_run` does with the conductor's
 * answers (a partially answered stage takes its defaults) and what the CLI does with `--answers`.
 */
export function completeAnswers(
  def: WorkflowDef,
  ctx: QuestionaryCtx,
  given: Answers,
): CompletedAnswers {
  let answers: Answers = { ...given };
  const seen = new Map<string, Question>();
  let missing: string[] = [];
  let inputs: Record<string, string> = {};
  // Every pass answers at least one more question or ends; the group count bounds the passes.
  for (let pass = 0; pass < 3 * (def.tuning?.groups.length ?? 0) + 2; pass++) {
    const q = buildQuestionary(def, ctx, answers);
    for (const question of q.questions) seen.set(question.id, question);
    const filled = fillAnswers(q.questions, answers);
    missing = filled.missing;
    inputs = filled.inputs;
    const grew = Object.keys(filled.answers).length > Object.keys(answers).length;
    answers = filled.answers;
    if (!grew) break;
  }
  return { answers, inputs, missing, questions: [...seen.values()] };
}
