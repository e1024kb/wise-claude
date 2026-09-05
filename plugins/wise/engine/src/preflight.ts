// Pre-flight questionary (D11/D12): the engine builds the questions, the harness asks them.
// Port of the tuning / step-select / profiles / inputs semantics of workflows.py
// (cmd_get_preflight, cmd_get_tuning, cmd_get_step_select, cmd_get_profiles,
// cmd_list_inputs) onto the v2 shape. Pure: no I/O.

import { PROFILE_LEVELS } from "./types.ts";
import type {
  Answers,
  Context,
  ProfileLevel,
  Question,
  QuestionOption,
  Step,
  TuningDefault,
  TuningGroup,
  WorkflowDef,
} from "./types.ts";
import { PROFILE_DEFAULT, isProfileLevel } from "./profile.ts";
import { listInputs } from "./defs.ts";

/** The `tuning.<group>` answer that keeps the group's (profile-adjusted) default. */
export const KEEP_DEFAULT = "default";

export type Questionary = { questions: Question[]; defaults: Answers };

export type QuestionaryCtx = {
  context?: Context;
  profile?: ProfileLevel;
  installedPlugins?: ReadonlySet<string>;
};

/** `claude / opus / high` style summary of a tuning value; `inherit` when empty. */
export function describeTuning(value: TuningDefault): string {
  const parts = [value.harness, value.model, value.effort].filter((p): p is string => Boolean(p));
  return parts.length ? parts.join(" / ") : "inherit";
}

/** Profile levels a workflow offers: the declared ones, else all three. */
export function profileLevels(def: WorkflowDef): ProfileLevel[] {
  const declared = PROFILE_LEVELS.filter((level) => def.profiles?.[level] !== undefined);
  return declared.length ? declared : [...PROFILE_LEVELS];
}

const PROFILE_BLURB: Record<ProfileLevel, string> = {
  low: "cheapest tiers, fewer retries",
  medium: "the workflow's declared defaults",
  max: "highest tiers, widest scope",
};

function profileQuestion(def: WorkflowDef, wanted: ProfileLevel | undefined): Question {
  const levels = profileLevels(def);
  const options: QuestionOption[] = levels.map((level) => ({
    value: level,
    label: level,
    description: def.profiles?.[level]?.description ?? PROFILE_BLURB[level],
  }));
  const fallback = levels.includes(PROFILE_DEFAULT) ? PROFILE_DEFAULT : (levels[0] as ProfileLevel);
  const chosen = wanted !== undefined && levels.includes(wanted) ? wanted : fallback;
  return {
    id: "profile",
    kind: "choice",
    label: "Budget profile for this run?",
    options,
    default: chosen,
  };
}

function tuningQuestion(group: TuningGroup): Question {
  const options: QuestionOption[] = [
    { value: KEEP_DEFAULT, label: `Keep default (${describeTuning(group.default)})` },
  ];
  if (!group.locked) {
    for (const preset of group.options ?? []) {
      const option: QuestionOption = { value: preset.id, label: preset.label ?? preset.id };
      option.description =
        preset.description ?? describeTuning({ ...group.default, ...preset.value });
      options.push(option);
    }
  }
  const q: Question = {
    id: `tuning.${group.id}`,
    kind: "choice",
    label: group.label ?? group.id,
    options,
    default: KEEP_DEFAULT,
  };
  if (group.locked) q.locked = true;
  return q;
}

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

/**
 * Build the questionary in order: `profile`, one `tuning.<group>` per group (locked groups
 * carry `locked: true` and only the default option), `step-select` when the workflow has
 * optional steps, and `input.<name>` per declared input with `from-context` pre-fill.
 */
export function buildQuestionary(def: WorkflowDef, ctx: QuestionaryCtx = {}): Questionary {
  const questions: Question[] = [];
  const defaults: Answers = {};
  const push = (q: Question): void => {
    questions.push(q);
    if (q.default !== undefined) defaults[q.id] = q.default;
  };

  push(profileQuestion(def, ctx.profile));
  for (const group of def.tuning?.groups ?? []) push(tuningQuestion(group));
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
  profile: ProfileLevel;
  /** Effective tuning per group: preset answer over profile override over group default. */
  tuning: Record<string, TuningDefault>;
  enabledSteps: Set<string>;
  inputs: Record<string, string>;
  caps: Record<string, number>;
};

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

/**
 * Merge answers into run parameters. Precedence per group: an explicit preset answer (unlocked
 * groups only) > `profiles.<level>.tuning.<group>` > `group.default`. Steps not selected in
 * `step-select` are disabled; every non-optional step is always enabled. Model / effort
 * clamping is `resolve`'s job, not done here.
 */
export function applyAnswers(def: WorkflowDef, answers: Answers): Applied {
  const levels = profileLevels(def);
  const wanted = answerString(answers.profile);
  const fallback = levels.includes(PROFILE_DEFAULT) ? PROFILE_DEFAULT : (levels[0] as ProfileLevel);
  const profile: ProfileLevel =
    wanted !== undefined && isProfileLevel(wanted) && levels.includes(wanted) ? wanted : fallback;
  const profileDef = def.profiles?.[profile];

  const tuning: Record<string, TuningDefault> = {};
  for (const group of def.tuning?.groups ?? []) {
    let value: TuningDefault = { ...group.default, ...profileDef?.tuning?.[group.id] };
    const answer = answerString(answers[`tuning.${group.id}`]);
    if (!group.locked && answer !== undefined && answer !== KEEP_DEFAULT) {
      const preset = (group.options ?? []).find((o) => o.id === answer);
      if (preset) value = { ...value, ...preset.value };
    }
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
