// Workflow definitions: locate (user root shadows bundled), load YAML, validate the v2
// schema (P2) with v1 migration hints, list, probe `requires`, and normalise inputs.
// Port of workflows.py: cmd_locate_def, load_yaml, _validate_step_defs, installed_plugins,
// cmd_probe_requires, cmd_list_defs, cmd_list_inputs, cmd_validate_input.

import { accessSync, constants, readdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import { parse as parseYaml } from "yaml";
import { PLUGIN_ROOT } from "./version.ts";
import { pluginDataRoot } from "./profile.ts";
import type { ProfileEnv } from "./profile.ts";
import {
  AUTH_MODES,
  EFFORTS,
  HARNESSES,
  PHASES,
  PROFILE_LEVELS,
  RUN_MODES,
  STEP_TYPES,
  TRIGGER_RULES,
} from "./types.ts";
import type {
  AgentStep,
  ApprovalStep,
  AskStep,
  BashStep,
  Effort,
  Harness,
  InputDef,
  JsonSchema,
  LocatedDef,
  ProfileDef,
  Profiles,
  Step,
  StepBase,
  StepSelect,
  StepType,
  TriggerRule,
  TuningDefault,
  TuningGroup,
  UnitsStep,
  ValidationIssue,
  WorkflowDef,
} from "./types.ts";

// ---- constants ---------------------------------------------------------------------

export const RESERVED_NAMES: ReadonlySet<string> = new Set([
  "list",
  "create",
  "run",
  "resume",
  "remove",
  "status",
]);

/** Step ids are hyphen-case and land in file names. */
export const STEP_ID_RE = /^[a-z][a-z0-9_-]*$/;
/** Tuning-group and step-select ids: like STEP_ID_RE without underscores. */
export const SLUG_RE = /^[a-z][a-z0-9-]*$/;
/** Cap names sit after a `cap_` prefix, so underscores are fine. */
export const CAP_RE = /^[a-z][a-z0-9_]*$/;
export const INPUT_NAME_RE = /^[a-z][a-z0-9_]*$/;
/** `from-context` paths (E1): `guidance`, `ticket[].<field>`, `links[]`, `decisions.<key>`. */
export const FROM_CONTEXT_RE =
  /^(guidance|ticket\[\]\.(ref|title|body|url)|links\[\]|decisions\.[A-Za-z0-9_-]+)$/;

const V1_STEP_TYPES: ReadonlySet<string> = new Set([
  "prompt",
  "skill",
  "interactive",
  "supervised-prompt",
]);

// ---- roots and locate ---------------------------------------------------------------

export type DefRoots = { userRoot: string; bundledRoot: string };

/** User root from the data dir (`<plugin data>/workflows/definitions`), bundled from PLUGIN_ROOT. */
export function defaultRoots(opts: ProfileEnv = {}): DefRoots {
  return {
    userRoot: join(pluginDataRoot(opts), "workflows", "definitions"),
    bundledRoot: join(PLUGIN_ROOT, "workflows"),
  };
}

function isFile(path: string): boolean {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}
function isDir(path: string): boolean {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/** Folder form has a workflow dir; the flat form has none (empty string), as in Python. */
function locatedAt(name: string, path: string, source: LocatedDef["source"]): LocatedDef {
  const dir = basename(path) === "workflow.yaml" ? dirname(path) : "";
  return { name, path, dir, source };
}

/**
 * Find a definition by name. User root shadows bundled; within a root the folder form
 * `<root>/<name>/workflow.yaml` wins over the flat form `<root>/<name>.yaml`.
 * Reserved names (`RESERVED_NAMES`) and unknown names return null.
 */
export function locateDef(name: string, roots: DefRoots): LocatedDef | null {
  if (RESERVED_NAMES.has(name)) return null;
  const order: [LocatedDef["source"], string][] = [
    ["user", roots.userRoot],
    ["bundled", roots.bundledRoot],
  ];
  for (const [source, root] of order) {
    const folder = join(root, name, "workflow.yaml");
    if (isFile(folder)) return locatedAt(name, folder, source);
    const flat = join(root, `${name}.yaml`);
    if (isFile(flat)) return locatedAt(name, flat, source);
  }
  return null;
}

/** Parse a YAML file; an empty document is `{}`. Throws on unreadable or malformed input. */
export function loadDef(path: string): unknown {
  const parsed: unknown = parseYaml(readFileSync(path, "utf8"));
  return parsed ?? {};
}

// ---- validation ---------------------------------------------------------------------

export type ValidateResult = { def?: WorkflowDef; issues: ValidationIssue[] };

type Rec = Record<string, unknown>;
function isRecord(v: unknown): v is Rec {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function isStringList(v: unknown): v is string[] {
  return Array.isArray(v) && v.every((x) => typeof x === "string");
}
function isPosInt(v: unknown): v is number {
  return typeof v === "number" && Number.isInteger(v) && v >= 1;
}
function oneOf<T extends string>(list: readonly T[], v: unknown): v is T {
  return typeof v === "string" && (list as readonly string[]).includes(v);
}
function show(v: unknown): string {
  return JSON.stringify(v) ?? String(v);
}

const V2_HINT =
  "set `version: 2`; v2 step types are agent | bash | approval | ask | units (see docs/wise/research-ts-engine.md P2)";

class Issues {
  readonly list: ValidationIssue[] = [];
  error(path: string, message: string, hint?: string): void {
    this.list.push(
      hint === undefined
        ? { level: "error", path, message }
        : { level: "error", path, message, hint },
    );
  }
  warn(path: string, message: string, hint?: string): void {
    this.list.push(
      hint === undefined
        ? { level: "warning", path, message }
        : { level: "warning", path, message, hint },
    );
  }
  get hasErrors(): boolean {
    return this.list.some((i) => i.level === "error");
  }
}

function warnUnknownKeys(iss: Issues, obj: Rec, path: string, known: readonly string[]): void {
  for (const key of Object.keys(obj)) {
    if (!known.includes(key))
      iss.warn(path ? `${path}.${key}` : key, `unknown key \`${key}\` is ignored`);
  }
}

function validateTuningDefault(iss: Issues, raw: unknown, path: string): TuningDefault | undefined {
  if (typeof raw === "string") {
    const [model = "", effort] = raw.split("/").map((s) => s.trim());
    const obj = effort
      ? `{ harness: claude, model: ${model}, effort: ${effort} }`
      : `{ harness: claude, model: ${model} }`;
    iss.error(path, `v1 string tuning value ${show(raw)}`, `write it as a mapping: ${obj}`);
    return undefined;
  }
  if (!isRecord(raw)) {
    iss.error(path, "expected a mapping with harness / model / effort");
    return undefined;
  }
  warnUnknownKeys(iss, raw, path, ["harness", "model", "effort"]);
  const out: TuningDefault = {};
  let ok = true;
  if (raw.harness !== undefined) {
    if (oneOf(HARNESSES, raw.harness)) out.harness = raw.harness;
    else {
      iss.error(`${path}.harness`, `harness must be one of ${HARNESSES.join(" | ")}`);
      ok = false;
    }
  }
  if (raw.model !== undefined) {
    if (typeof raw.model === "string" && raw.model) out.model = raw.model;
    else {
      iss.error(`${path}.model`, "model must be a non-empty string");
      ok = false;
    }
  }
  if (raw.effort !== undefined) {
    if (oneOf(EFFORTS, raw.effort)) out.effort = raw.effort;
    else {
      iss.error(`${path}.effort`, `effort must be one of ${EFFORTS.join(" | ")}`);
      ok = false;
    }
  }
  return ok ? out : undefined;
}

function validateHarnessList(iss: Issues, raw: unknown, path: string): Harness[] | undefined {
  if (!Array.isArray(raw)) {
    iss.error(path, "expected a list of harnesses");
    return undefined;
  }
  const out: Harness[] = [];
  for (const [i, h] of raw.entries()) {
    if (oneOf(HARNESSES, h)) out.push(h);
    else iss.error(`${path}[${i}]`, `harness must be one of ${HARNESSES.join(" | ")}`);
  }
  return out;
}

function validateTuning(iss: Issues, raw: unknown): TuningGroup[] {
  const groups: TuningGroup[] = [];
  if (raw === undefined || raw === null) return groups;
  if (!isRecord(raw)) {
    iss.error("tuning", "expected a mapping with `groups:`");
    return groups;
  }
  warnUnknownKeys(iss, raw, "tuning", ["groups"]);
  const rawGroups = raw.groups ?? [];
  if (!Array.isArray(rawGroups)) {
    iss.error("tuning.groups", "expected a list of groups");
    return groups;
  }
  const seen = new Set<string>();
  for (const [i, g] of rawGroups.entries()) {
    const p = `tuning.groups[${i}]`;
    if (!isRecord(g)) {
      iss.error(p, "expected a mapping");
      continue;
    }
    warnUnknownKeys(iss, g, p, [
      "id",
      "label",
      "description",
      "default",
      "fallback",
      "locked",
      "allow-api",
      "options",
      "steps",
    ]);
    const id = typeof g.id === "string" ? g.id : "";
    if (!SLUG_RE.test(id)) {
      iss.error(`${p}.id`, `tuning group id ${show(g.id)} must match ${SLUG_RE.source}`);
      continue;
    }
    if (seen.has(id)) {
      iss.error(`${p}.id`, `duplicate tuning group id ${show(id)}`);
      continue;
    }
    seen.add(id);
    if (g.steps !== undefined) {
      iss.error(
        `${p}.steps`,
        "v1 `steps:` binding on a tuning group",
        `remove \`steps:\`; set \`group: ${id}\` on each of those steps and give the group a \`default: { harness, model, effort }\``,
      );
    }
    let def: TuningDefault | undefined;
    if (g.default === undefined) {
      iss.error(`${p}.default`, "tuning group needs a `default: { harness, model, effort }`");
    } else {
      def = validateTuningDefault(iss, g.default, `${p}.default`);
    }
    const group: TuningGroup = { id, default: def ?? {} };
    if (g.label !== undefined) {
      if (typeof g.label === "string") group.label = g.label;
      else iss.error(`${p}.label`, "label must be a string");
    }
    if (g.description !== undefined) {
      if (typeof g.description === "string") group.description = g.description;
      else iss.error(`${p}.description`, "description must be a string");
    }
    if (g.fallback !== undefined) {
      const fb = validateHarnessList(iss, g.fallback, `${p}.fallback`);
      if (fb) group.fallback = fb;
    }
    if (g.locked !== undefined) {
      if (typeof g.locked === "boolean") group.locked = g.locked;
      else iss.error(`${p}.locked`, "locked must be a boolean");
    }
    if (g["allow-api"] !== undefined) {
      if (typeof g["allow-api"] === "boolean") group["allow-api"] = g["allow-api"];
      else iss.error(`${p}.allow-api`, "allow-api must be a boolean");
    }
    if (g.options !== undefined) {
      if (!Array.isArray(g.options)) {
        iss.error(`${p}.options`, "options must be a list of presets");
      } else {
        const opts: NonNullable<TuningGroup["options"]> = [];
        const seenOpt = new Set<string>();
        for (const [j, o] of g.options.entries()) {
          const op = `${p}.options[${j}]`;
          if (!isRecord(o)) {
            iss.error(op, "expected a mapping with id and value");
            continue;
          }
          warnUnknownKeys(iss, o, op, ["id", "label", "description", "value"]);
          const oid = typeof o.id === "string" ? o.id : "";
          if (!SLUG_RE.test(oid)) {
            iss.error(`${op}.id`, `preset id ${show(o.id)} must match ${SLUG_RE.source}`);
            continue;
          }
          if (oid === "default" || seenOpt.has(oid)) {
            iss.error(`${op}.id`, `preset id ${show(oid)} is reserved or duplicated`);
            continue;
          }
          seenOpt.add(oid);
          const value = validateTuningDefault(iss, o.value, `${op}.value`);
          if (!value) continue;
          const preset: (typeof opts)[number] = { id: oid, value };
          if (typeof o.label === "string") preset.label = o.label;
          if (typeof o.description === "string") preset.description = o.description;
          opts.push(preset);
        }
        group.options = opts;
      }
    }
    groups.push(group);
  }
  return groups;
}

function validateProfiles(iss: Issues, raw: unknown, groupIds: ReadonlySet<string>): Profiles {
  const out: Profiles = {};
  if (raw === undefined || raw === null) return out;
  if (!isRecord(raw)) {
    iss.error("profiles", "expected a mapping keyed by low | medium | max");
    return out;
  }
  for (const [level, entryRaw] of Object.entries(raw)) {
    const p = `profiles.${level}`;
    if (!oneOf(PROFILE_LEVELS, level)) {
      iss.error(p, `profile level ${show(level)} must be one of ${PROFILE_LEVELS.join(" | ")}`);
      continue;
    }
    const entry = entryRaw ?? {};
    if (!isRecord(entry)) {
      iss.error(p, "expected a mapping (an empty one means the workflow defaults)");
      continue;
    }
    warnUnknownKeys(iss, entry, p, [
      "tuning",
      "caps",
      "description",
      "step-preset",
      "skip",
      "team-mode",
    ]);
    for (const v1key of ["step-preset", "skip"] as const) {
      if (entry[v1key] !== undefined) {
        iss.error(
          `${p}.${v1key}`,
          `v1 \`${v1key}:\` on a profile`,
          "drop it; v2 has no presets, the harness asks `step-select` as one multi-select over the optional steps",
        );
      }
    }
    if (entry["team-mode"] !== undefined) {
      iss.error(
        `${p}.team-mode`,
        "v1 `team-mode:` on a profile",
        "drop it; v2 has no agent teams, one agent per step",
      );
    }
    const prof: ProfileDef = {};
    if (entry.description !== undefined) {
      if (typeof entry.description === "string") prof.description = entry.description;
      else iss.error(`${p}.description`, "description must be a string");
    }
    const tuning = entry.tuning ?? {};
    if (!isRecord(tuning)) {
      iss.error(
        `${p}.tuning`,
        "expected a mapping of tuning group id to { harness, model, effort }",
      );
    } else {
      const t: Record<string, TuningDefault> = {};
      for (const [gid, value] of Object.entries(tuning)) {
        const tp = `${p}.tuning.${gid}`;
        if (!groupIds.has(gid)) {
          iss.error(tp, `unknown tuning group ${show(gid)}`);
          continue;
        }
        if (value === "default") {
          iss.error(
            tp,
            'v1 `"default"` tuning value',
            "omit the group from the profile to keep its default",
          );
          continue;
        }
        const td = validateTuningDefault(iss, value, tp);
        if (td) t[gid] = td;
      }
      prof.tuning = t;
    }
    const caps = entry.caps ?? {};
    if (!isRecord(caps)) {
      iss.error(`${p}.caps`, "expected a mapping of cap name to positive integer");
    } else {
      const c: Record<string, number> = {};
      for (const [name, value] of Object.entries(caps)) {
        if (!CAP_RE.test(name)) {
          iss.error(`${p}.caps.${name}`, `cap name ${show(name)} must match ${CAP_RE.source}`);
          continue;
        }
        if (!isPosInt(value)) {
          iss.error(
            `${p}.caps.${name}`,
            `cap ${show(name)} must be a positive integer, got ${show(value)}`,
          );
          continue;
        }
        c[name] = value;
      }
      prof.caps = c;
    }
    out[level] = prof;
  }
  return out;
}

function compileRegex(iss: Issues, raw: unknown, path: string): string | undefined {
  if (typeof raw !== "string" || !raw) {
    iss.error(path, "expected a non-empty regex string");
    return undefined;
  }
  try {
    RegExp(raw);
  } catch (e) {
    iss.error(path, `invalid regex: ${(e as Error).message}`);
    return undefined;
  }
  return raw;
}

function validateInputs(iss: Issues, raw: unknown): InputDef[] {
  const out: InputDef[] = [];
  if (raw === undefined || raw === null) return out;
  if (!Array.isArray(raw)) {
    iss.error("inputs", "expected a list of inputs");
    return out;
  }
  const seen = new Set<string>();
  for (const [i, entry] of raw.entries()) {
    const p = `inputs[${i}]`;
    if (!isRecord(entry)) {
      iss.error(p, "expected a mapping");
      continue;
    }
    warnUnknownKeys(iss, entry, p, [
      "name",
      "prompt",
      "description",
      "optional",
      "default",
      "from-context",
      "validate",
      "extract",
      "options",
    ]);
    const name = entry.name;
    if (typeof name !== "string" || !INPUT_NAME_RE.test(name)) {
      iss.error(`${p}.name`, `input name ${show(name)} must match ${INPUT_NAME_RE.source}`);
      continue;
    }
    if (seen.has(name)) {
      iss.error(`${p}.name`, `duplicate input ${show(name)}`);
      continue;
    }
    seen.add(name);
    const input: InputDef = { name };
    if (entry.options !== undefined) {
      const values = Array.isArray(entry.options)
        ? entry.options
            .map((o) => (isRecord(o) ? String(o.value ?? "") : String(o)))
            .filter(Boolean)
        : [];
      const alt = values.length
        ? `validate: "^(${values.map(escapeRegex).join("|")})$"`
        : "a `validate:` regex";
      iss.error(
        `${p}.options`,
        `v1 choice input ${show(name)}`,
        `v2 inputs are text: keep \`default:\` and add ${alt}, or move the choice into an \`ask\` step with \`options:\``,
      );
    }
    if (entry.prompt !== undefined) {
      if (typeof entry.prompt === "string") input.prompt = entry.prompt;
      else iss.error(`${p}.prompt`, "prompt must be a string");
    }
    if (entry.description !== undefined) {
      if (typeof entry.description === "string") input.description = entry.description;
      else iss.error(`${p}.description`, "description must be a string");
    }
    if (entry.optional !== undefined) {
      if (typeof entry.optional === "boolean") input.optional = entry.optional;
      else iss.error(`${p}.optional`, "optional must be a boolean");
    }
    if (entry.default !== undefined && entry.default !== null) {
      if (
        typeof entry.default === "string" ||
        typeof entry.default === "number" ||
        typeof entry.default === "boolean"
      ) {
        input.default = String(entry.default);
      } else iss.error(`${p}.default`, "default must be a scalar");
    }
    if (entry["from-context"] !== undefined) {
      const fc = entry["from-context"];
      if (typeof fc === "string" && FROM_CONTEXT_RE.test(fc)) input["from-context"] = fc;
      else
        iss.error(
          `${p}.from-context`,
          `from-context ${show(fc)} must be one of guidance | ticket[].ref | ticket[].title | ticket[].body | ticket[].url | links[] | decisions.<key>`,
        );
    }
    if (entry.validate !== undefined) {
      const re = compileRegex(iss, entry.validate, `${p}.validate`);
      if (re) input.validate = re;
    }
    if (entry.extract !== undefined) {
      const re = compileRegex(iss, entry.extract, `${p}.extract`);
      if (re) input.extract = re;
    }
    out.push(input);
  }
  return out;
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function validateStepSelect(
  iss: Issues,
  raw: unknown,
  stepIds: ReadonlySet<string>,
): StepSelect | undefined {
  if (raw === undefined || raw === null) return undefined;
  if (!isRecord(raw)) {
    iss.error("step-select", "expected a mapping with `prompt:` and `optional:`");
    return undefined;
  }
  warnUnknownKeys(iss, raw, "step-select", ["prompt", "optional", "presets"]);
  const out: StepSelect = {};
  if (raw.prompt !== undefined) {
    if (typeof raw.prompt === "string") out.prompt = raw.prompt;
    else iss.error("step-select.prompt", "prompt must be a string");
  }
  if (raw.presets !== undefined) {
    iss.error(
      "step-select.presets",
      "v1 `presets:`",
      "drop it; v2 has no presets, the harness asks `step-select` as one multi-select over `optional:`",
    );
  }
  if (raw.optional !== undefined) {
    if (!Array.isArray(raw.optional)) {
      iss.error("step-select.optional", "expected a list of step ids");
    } else {
      const ids: string[] = [];
      const seen = new Set<string>();
      for (const [i, entry] of raw.optional.entries()) {
        const p = `step-select.optional[${i}]`;
        if (isRecord(entry)) {
          const steps = Array.isArray(entry.steps)
            ? entry.steps.map(String)
            : [String(entry.id ?? "")];
          iss.error(
            p,
            `v1 step-select entry ${show(entry.id)} (label / steps / ask-group)`,
            `list step ids directly: \`optional: [${steps.join(", ")}]\`; put the wording in each step's \`description:\``,
          );
          continue;
        }
        if (typeof entry !== "string") {
          iss.error(p, "expected a step id");
          continue;
        }
        if (!stepIds.has(entry)) {
          iss.error(p, `unknown step ${show(entry)}`);
          continue;
        }
        if (seen.has(entry)) {
          iss.error(p, `duplicate optional step ${show(entry)}`);
          continue;
        }
        seen.add(entry);
        ids.push(entry);
      }
      out.optional = ids;
    }
  }
  return out;
}

// ---- steps --------------------------------------------------------------------------------

const STEP_BASE_KEYS = [
  "id",
  "type",
  "group",
  "depends_on",
  "trigger-rule",
  "when",
  "description",
  "optional",
  "harness",
  "model",
  "effort",
  "auth",
  "fallback",
  "mode",
  "resume",
  "max_turns",
  "timeout",
  "stale_after",
  "allowed_tools",
  "allow-api",
] as const;
const STEP_KEYS: Record<StepType, readonly string[]> = {
  agent: [...STEP_BASE_KEYS, "prompt", "skill", "schema", "outputs", "until"],
  bash: [...STEP_BASE_KEYS, "run", "outputs"],
  approval: [...STEP_BASE_KEYS, "message"],
  ask: [...STEP_BASE_KEYS, "message", "options", "allow_text", "output"],
  units: [...STEP_BASE_KEYS, "pipeline", "items", "groups", "caps", "parallel", "reviewers"],
};
/** v1 step keys and the hint each one gets, independent of the step type. */
const V1_STEP_KEY_HINTS: Record<string, string> = {
  max_iterations:
    "drop it; use `schema:` for structured output (E9) and `max_turns:` to cap turns (E11)",
  agent:
    "drop the roster role binding; fold the role into `prompt:` (a Claude child loads the plugin's agents)",
  payload: "fold the arguments into `prompt:` (`skill:` sugar emits `Run /<skill>`)",
  command: "rename to `run:`",
  success: "drop it; a bash step succeeds on exit code 0, assert on output through `outputs:`",
  cwd: "drop it; steps run in the run's cwd (`cd` inside `run:` when needed)",
  question: "rename to `message:`",
  header: "drop it; the harness renders the gate",
  skip_label: "drop it; put the skip wording in `options:`",
};

type StepScan = {
  ids: Set<string>;
  groupIds: ReadonlySet<string>;
  capNames: ReadonlySet<string>;
};

function v1StepKeyHints(iss: Issues, step: Rec, p: string): void {
  for (const [key, hint] of Object.entries(V1_STEP_KEY_HINTS)) {
    if (step[key] !== undefined) iss.error(`${p}.${key}`, `v1 step key \`${key}\``, hint);
  }
  if (step.until !== undefined && (step.type === undefined || step.type !== "agent")) {
    const outputs = isStringList(step.outputs) ? step.outputs : [];
    iss.error(`${p}.until`, "v1 `until:` regex capture", schemaHint(outputs));
  }
}

function schemaHint(outputs: readonly string[]): string {
  const props = outputs.length
    ? outputs.map((o) => `${o}: { type: string }`).join(", ")
    : "<name>: { type: string }";
  return `replace with \`schema: { type: object, properties: { ${props} }, required: [${outputs.join(", ")}] }\` and keep \`outputs:\` as the names to copy`;
}

function validateStepBase(
  iss: Issues,
  step: Rec,
  p: string,
  id: string,
  type: StepType,
  scan: StepScan,
): StepBase {
  const base: StepBase = { id, type };
  if (step.group !== undefined) {
    if (typeof step.group !== "string" || !scan.groupIds.has(step.group)) {
      iss.error(`${p}.group`, `group ${show(step.group)} does not name a tuning group`);
    } else {
      base.group = step.group;
      if (type !== "agent" && type !== "units")
        iss.warn(`${p}.group`, `group has no effect on a ${type} step`);
    }
  }
  if (step.depends_on !== undefined) {
    if (!isStringList(step.depends_on)) {
      iss.error(`${p}.depends_on`, "expected a list of step ids");
    } else {
      for (const dep of step.depends_on) {
        if (dep === id) iss.error(`${p}.depends_on`, `step ${show(id)} depends on itself`);
        else if (!scan.ids.has(dep)) iss.error(`${p}.depends_on`, `unknown step ${show(dep)}`);
      }
      base.depends_on = step.depends_on;
    }
  }
  if (step["trigger-rule"] !== undefined) {
    const tr = step["trigger-rule"];
    if (oneOf(TRIGGER_RULES, tr)) base["trigger-rule"] = tr as TriggerRule;
    else iss.error(`${p}.trigger-rule`, `trigger-rule must be one of ${TRIGGER_RULES.join(" | ")}`);
  }
  if (step.when !== undefined) {
    if (Array.isArray(step.when)) {
      iss.error(
        `${p}.when`,
        "v1 list of `when:` conditions",
        `join them into one expression: \`when: "${step.when.map(String).join(" && ")}"\``,
      );
    } else if (typeof step.when === "string") base.when = step.when;
    else iss.error(`${p}.when`, "when must be an expression string");
  }
  if (step.description !== undefined) {
    if (typeof step.description === "string") base.description = step.description;
    else iss.error(`${p}.description`, "description must be a string");
  }
  if (step.optional !== undefined) {
    if (typeof step.optional === "boolean") base.optional = step.optional;
    else iss.error(`${p}.optional`, "optional must be a boolean");
  }
  if (step.harness !== undefined) {
    if (oneOf(HARNESSES, step.harness)) base.harness = step.harness;
    else iss.error(`${p}.harness`, `harness must be one of ${HARNESSES.join(" | ")}`);
  }
  if (step.model !== undefined) {
    if (typeof step.model === "string" && step.model) base.model = step.model;
    else iss.error(`${p}.model`, "model must be a non-empty string");
  }
  if (step.effort !== undefined) {
    if (oneOf(EFFORTS, step.effort)) base.effort = step.effort as Effort;
    else iss.error(`${p}.effort`, `effort must be one of ${EFFORTS.join(" | ")}`);
  }
  if (step.auth !== undefined) {
    if (oneOf(AUTH_MODES, step.auth)) base.auth = step.auth;
    else iss.error(`${p}.auth`, `auth must be one of ${AUTH_MODES.join(" | ")}`);
  }
  if (step.fallback !== undefined) {
    const fb = validateHarnessList(iss, step.fallback, `${p}.fallback`);
    if (fb) base.fallback = fb;
  }
  if (step.mode !== undefined) {
    if (oneOf(RUN_MODES, step.mode)) base.mode = step.mode;
    else iss.error(`${p}.mode`, `mode must be one of ${RUN_MODES.join(" | ")}`);
  }
  if (step.resume !== undefined) {
    if (step.resume === "unit" || step.resume === "fresh") base.resume = step.resume;
    else iss.error(`${p}.resume`, "resume must be unit | fresh");
  }
  if (step.max_turns !== undefined) {
    if (isPosInt(step.max_turns)) base.max_turns = step.max_turns;
    else iss.error(`${p}.max_turns`, "max_turns must be a positive integer");
  }
  if (step.timeout !== undefined) {
    if (typeof step.timeout === "number" && step.timeout > 0) base.timeout = step.timeout;
    else iss.error(`${p}.timeout`, "timeout must be a positive number of seconds");
  }
  if (step.stale_after !== undefined) {
    if (typeof step.stale_after === "number" && step.stale_after > 0) {
      base.stale_after = step.stale_after;
    } else iss.error(`${p}.stale_after`, "stale_after must be a positive number of seconds");
  }
  if (step.allowed_tools !== undefined) {
    const list = step.allowed_tools;
    if (Array.isArray(list) && list.every((v) => typeof v === "string" && v.trim())) {
      base.allowed_tools = list as string[];
    } else iss.error(`${p}.allowed_tools`, "allowed_tools must be a list of non-empty strings");
  }
  if (step["allow-api"] !== undefined) {
    if (typeof step["allow-api"] === "boolean") base["allow-api"] = step["allow-api"];
    else iss.error(`${p}.allow-api`, "allow-api must be a boolean");
  }
  return base;
}

function requireString(iss: Issues, step: Rec, p: string, key: string): string | undefined {
  const v = step[key];
  if (typeof v === "string" && v.trim()) return v;
  iss.error(`${p}.${key}`, `${key} must be a non-empty string`);
  return undefined;
}

function validateAgentStep(
  iss: Issues,
  step: Rec,
  p: string,
  base: StepBase,
): AgentStep | undefined {
  let prompt: string | undefined;
  let skill: string | undefined;
  if (step.skill !== undefined) {
    if (typeof step.skill !== "string" || !step.skill.trim()) {
      iss.error(`${p}.skill`, "skill must be a non-empty skill name");
    } else {
      skill = step.skill.trim().replace(/^\//, "");
      if (step.prompt !== undefined)
        iss.error(`${p}.prompt`, "`skill:` and `prompt:` are exclusive; `skill:` emits the prompt");
      if (base.harness !== undefined && base.harness !== "claude") {
        iss.error(
          `${p}.harness`,
          "`skill:` forces `harness: claude` (only a Claude child loads plugin skills)",
        );
      }
      prompt = `Run /${skill}`;
      base.harness = "claude";
    }
  } else {
    prompt = requireString(iss, step, p, "prompt");
  }
  const out: AgentStep = { ...base, type: "agent", prompt: prompt ?? "" };
  if (skill !== undefined) out.skill = skill;
  let schema: JsonSchema | undefined;
  if (step.schema !== undefined) {
    if (isRecord(step.schema)) {
      schema = step.schema;
      out.schema = schema;
    } else iss.error(`${p}.schema`, "schema must be a JSON-schema mapping");
  }
  if (step.until !== undefined) {
    if (typeof step.until === "string") {
      out.until = step.until;
      iss.warn(
        `${p}.until`,
        "`until:` is deprecated and accepted for one release",
        schemaHint(isStringList(step.outputs) ? step.outputs : []),
      );
    } else iss.error(`${p}.until`, "until must be a regex string");
  }
  if (step.outputs !== undefined) {
    if (!isStringList(step.outputs)) {
      iss.error(`${p}.outputs`, "outputs must be a list of names");
    } else {
      out.outputs = step.outputs;
      if (schema === undefined && out.until === undefined) {
        iss.error(
          `${p}.outputs`,
          "outputs need a `schema:` to be copied from",
          schemaHint(step.outputs),
        );
      } else if (schema && isRecord(schema.properties)) {
        for (const name of step.outputs) {
          if (!(name in schema.properties))
            iss.error(`${p}.outputs`, `output ${show(name)} is not a schema property`);
        }
      }
    }
  }
  return prompt === undefined ? undefined : out;
}

function validateBashStep(iss: Issues, step: Rec, p: string, base: StepBase): BashStep | undefined {
  const run = requireString(iss, step, p, "run");
  const out: BashStep = { ...base, type: "bash", run: run ?? "" };
  if (step.outputs !== undefined) {
    if (isStringList(step.outputs)) out.outputs = step.outputs;
    else iss.error(`${p}.outputs`, "outputs must be a list of names");
  }
  return run === undefined ? undefined : out;
}

function validateApprovalStep(
  iss: Issues,
  step: Rec,
  p: string,
  base: StepBase,
): ApprovalStep | undefined {
  const message = requireString(iss, step, p, "message");
  return message === undefined ? undefined : { ...base, type: "approval", message };
}

function validateAskStep(iss: Issues, step: Rec, p: string, base: StepBase): AskStep | undefined {
  const message = requireString(iss, step, p, "message");
  const out: AskStep = { ...base, type: "ask", message: message ?? "" };
  if (step.options !== undefined) {
    if (isStringList(step.options) && step.options.length) out.options = step.options;
    else iss.error(`${p}.options`, "options must be a non-empty list of strings");
  }
  if (step.allow_text !== undefined) {
    if (typeof step.allow_text === "boolean") out.allow_text = step.allow_text;
    else iss.error(`${p}.allow_text`, "allow_text must be a boolean");
  }
  if (step.output !== undefined) {
    if (typeof step.output === "string" && CAP_RE.test(step.output)) out.output = step.output;
    else iss.error(`${p}.output`, `output must match ${CAP_RE.source}`);
  }
  return message === undefined ? undefined : out;
}

function validateUnitsStep(
  iss: Issues,
  step: Rec,
  p: string,
  base: StepBase,
  scan: StepScan,
): UnitsStep | undefined {
  let ok = true;
  if (step.pipeline !== "ticket" && step.pipeline !== "plan") {
    iss.error(`${p}.pipeline`, "pipeline must be ticket | plan");
    ok = false;
  }
  const items = requireString(iss, step, p, "items");
  if (items === undefined) ok = false;
  const groups: Record<string, string> = {};
  if (!isRecord(step.groups) || Object.keys(step.groups).length === 0) {
    iss.error(`${p}.groups`, "groups must map each phase to a tuning group id");
    ok = false;
  } else {
    for (const [phase, gid] of Object.entries(step.groups)) {
      if (!(PHASES as readonly string[]).includes(phase))
        iss.warn(`${p}.groups.${phase}`, `unknown phase ${show(phase)}`);
      if (typeof gid !== "string" || !scan.groupIds.has(gid)) {
        iss.error(`${p}.groups.${phase}`, `group ${show(gid)} does not name a tuning group`);
        ok = false;
      } else groups[phase] = gid;
    }
  }
  const out: UnitsStep = {
    ...base,
    type: "units",
    pipeline: step.pipeline === "plan" ? "plan" : "ticket",
    items: items ?? "",
    groups,
  };
  if (step.caps !== undefined) {
    if (!isStringList(step.caps)) {
      iss.error(`${p}.caps`, "caps must be a list of cap names");
    } else {
      for (const cap of step.caps) {
        if (!CAP_RE.test(cap))
          iss.error(`${p}.caps`, `cap name ${show(cap)} must match ${CAP_RE.source}`);
        else if (!scan.capNames.has(cap))
          iss.warn(`${p}.caps`, `cap ${show(cap)} is not set by any profile`);
      }
      out.caps = step.caps;
    }
  }
  if (step.parallel !== undefined) {
    if (isPosInt(step.parallel)) out.parallel = step.parallel;
    else iss.error(`${p}.parallel`, "parallel must be a positive integer");
  }
  if (step.reviewers !== undefined) {
    if (isStringList(step.reviewers)) out.reviewers = step.reviewers;
    else iss.error(`${p}.reviewers`, "reviewers must be a list of GitHub logins");
  }
  return ok ? out : undefined;
}

function v1TypeHint(type: string, step: Rec): string {
  switch (type) {
    case "prompt":
      return "rename to `type: agent`";
    case "skill":
      return `use \`type: agent\` with \`skill: ${typeof step.skill === "string" ? step.skill : "<skill>"}\` (sugar for \`prompt: "Run /<skill>"\`, harness claude)`;
    default:
      return "use `type: units` for the ticket / plan loop (D14, engine-side) or `type: agent` for a single child; loops live in the engine";
  }
}

function validateSteps(
  iss: Issues,
  raw: unknown,
  groupIds: ReadonlySet<string>,
  capNames: ReadonlySet<string>,
): Step[] {
  const out: Step[] = [];
  if (!Array.isArray(raw) || raw.length === 0) {
    iss.error("steps", "expected a non-empty list of steps");
    return out;
  }
  // First pass: ids, so depends_on can be checked against every step.
  const ids = new Set<string>();
  const seenIds = new Set<string>();
  for (const [i, step] of raw.entries()) {
    const p = `steps[${i}]`;
    if (!isRecord(step) || step.id === undefined || typeof step.type !== "string" || !step.type) {
      iss.error(p, `step is missing id or type: ${show(step)}`);
      continue;
    }
    if (typeof step.id !== "string" || !STEP_ID_RE.test(step.id)) {
      iss.error(`${p}.id`, `step id ${show(step.id)} must match ${STEP_ID_RE.source}`);
      continue;
    }
    if (seenIds.has(step.id)) iss.error(`${p}.id`, `duplicate step id ${show(step.id)}`);
    seenIds.add(step.id);
    ids.add(step.id);
  }
  const scan: StepScan = { ids, groupIds, capNames };
  for (const [i, step] of raw.entries()) {
    const p = `steps[${i}]`;
    if (
      !isRecord(step) ||
      typeof step.id !== "string" ||
      !STEP_ID_RE.test(step.id) ||
      typeof step.type !== "string"
    )
      continue;
    const id = step.id;
    const type = step.type;
    if (V1_STEP_TYPES.has(type)) {
      iss.error(
        `${p}.type`,
        `v1 step type ${show(type)} on step ${show(id)}`,
        v1TypeHint(type, step),
      );
      v1StepKeyHints(iss, step, p);
      continue;
    }
    if (!oneOf(STEP_TYPES, type)) {
      iss.error(
        `${p}.type`,
        `unknown step type ${show(type)}; expected ${STEP_TYPES.join(" | ")}`,
        V2_HINT,
      );
      continue;
    }
    v1StepKeyHints(iss, step, p);
    warnUnknownKeys(iss, step, p, [...STEP_KEYS[type], ...Object.keys(V1_STEP_KEY_HINTS)]);
    const base = validateStepBase(iss, step, p, id, type, scan);
    let typed: Step | undefined;
    switch (type) {
      case "agent":
        typed = validateAgentStep(iss, step, p, base);
        break;
      case "bash":
        typed = validateBashStep(iss, step, p, base);
        break;
      case "approval":
        typed = validateApprovalStep(iss, step, p, base);
        break;
      case "ask":
        typed = validateAskStep(iss, step, p, base);
        break;
      case "units":
        typed = validateUnitsStep(iss, step, p, base, scan);
        break;
    }
    if (typed) out.push(typed);
  }
  return out;
}

// ---- top level ---------------------------------------------------------------------------

const TOP_KEYS = [
  "version",
  "name",
  "description",
  "author",
  "project-selection",
  "preflight",
  "requires",
  "tuning",
  "profiles",
  "inputs",
  "step-select",
  "steps",
  "agents",
] as const;

function validatePreflight(iss: Issues, raw: unknown): WorkflowDef["preflight"] {
  if (raw === undefined || raw === null) return undefined;
  if (!isRecord(raw)) {
    iss.error("preflight", "expected a mapping");
    return undefined;
  }
  warnUnknownKeys(iss, raw, "preflight", [
    "control-mode",
    "worktree",
    "rename_session",
    "tuning",
    "step-select",
  ]);
  for (const key of ["rename_session", "tuning", "step-select"] as const) {
    if (raw[key] !== undefined) {
      iss.error(
        `preflight.${key}`,
        `v1 pre-flight pin \`${key}\``,
        "drop it; v2 always builds the questionary from `tuning:` / `step-select:` and the harness renames sessions (D11)",
      );
    }
  }
  const out: NonNullable<WorkflowDef["preflight"]> = {};
  const cm = raw["control-mode"];
  if (cm !== undefined) {
    if (cm === "synchronous" || cm === "interactive") out["control-mode"] = cm;
    else if (cm === "wave-sync" || cm === "auto-advance") {
      iss.error(
        "preflight.control-mode",
        `v1 control mode ${show(cm)}`,
        "use `control-mode: interactive` (gates pause the run)",
      );
    } else if (cm === "prompt") {
      iss.error(
        "preflight.control-mode",
        'v1 control mode "prompt"',
        "pick `synchronous` or `interactive`; v2 does not ask the control mode",
      );
    } else iss.error("preflight.control-mode", "control-mode must be synchronous | interactive");
  }
  const wt = raw.worktree;
  if (wt !== undefined) {
    if (wt === "current" || wt === "new") out.worktree = wt;
    else if (wt === "prompt") {
      iss.error(
        "preflight.worktree",
        'v1 worktree "prompt"',
        "pick `current` or `new`; v2 does not ask the worktree",
      );
    } else iss.error("preflight.worktree", "worktree must be current | new");
  }
  return out;
}

function validateRequires(iss: Issues, raw: unknown): WorkflowDef["requires"] {
  if (raw === undefined || raw === null) return undefined;
  if (Array.isArray(raw)) {
    const plugins: string[] = [];
    for (const entry of raw) {
      if (!isRecord(entry)) continue;
      if (typeof entry.plugin === "string") plugins.push(entry.plugin);
      else if (typeof entry.skill === "string")
        plugins.push(entry.skill.split(":", 1)[0] ?? entry.skill);
    }
    iss.error(
      "requires",
      "v1 `requires:` list of { plugin } / { skill } entries",
      `write \`requires: { plugins: [${plugins.join(", ")}] }\` (a skill requirement names its owning plugin)`,
    );
    return undefined;
  }
  if (!isRecord(raw)) {
    iss.error("requires", "expected a mapping with `plugins:` and/or `tools:`");
    return undefined;
  }
  warnUnknownKeys(iss, raw, "requires", ["plugins", "tools"]);
  const out: NonNullable<WorkflowDef["requires"]> = {};
  if (raw.plugins !== undefined) {
    if (isStringList(raw.plugins)) out.plugins = raw.plugins;
    else iss.error("requires.plugins", "expected a list of plugin names");
  }
  if (raw.tools !== undefined) {
    if (isStringList(raw.tools)) out.tools = raw.tools;
    else iss.error("requires.tools", "expected a list of executable names");
  }
  return out;
}

/**
 * Validate a parsed workflow document against P2. Returns every issue found; `def` is set
 * only when there are no errors. v1 constructs produce errors whose `hint` names the v2
 * replacement.
 */
export function validateDef(raw: unknown, path: string): ValidateResult {
  const iss = new Issues();
  if (!isRecord(raw)) {
    iss.error("", `${path}: expected a YAML mapping at the top level`);
    return { issues: iss.list };
  }
  warnUnknownKeys(iss, raw, "", TOP_KEYS);

  if (raw.version === undefined) iss.error("version", "missing `version`", V2_HINT);
  else if (raw.version === 1) iss.error("version", "v1 workflow", V2_HINT);
  else if (raw.version !== 2)
    iss.error("version", `unsupported version ${show(raw.version)}`, V2_HINT);

  const name = raw.name;
  if (typeof name !== "string" || !name.trim())
    iss.error("name", "name must be a non-empty string");
  if (raw.description !== undefined && typeof raw.description !== "string") {
    iss.error("description", "description must be a string");
  }
  if (raw.agents !== undefined) {
    iss.error(
      "agents",
      "v1 workflow-level `agents:` policy",
      "drop it; v2 has no roster routing, each `agent` step carries its own prompt",
    );
  }

  let projectSelection: WorkflowDef["project-selection"];
  const ps = raw["project-selection"];
  if (ps !== undefined) {
    if (ps === "current" || ps === "ask" || ps === "none") projectSelection = ps;
    else if (ps === "prompt")
      iss.error("project-selection", 'v1 value "prompt"', "use `project-selection: ask`");
    else if (ps === "any")
      iss.error("project-selection", 'v1 value "any"', "use `project-selection: none`");
    else iss.error("project-selection", "project-selection must be current | ask | none");
  }

  const preflight = validatePreflight(iss, raw.preflight);
  const requires = validateRequires(iss, raw.requires);
  const groups = validateTuning(iss, raw.tuning);
  const groupIds = new Set(groups.map((g) => g.id));
  const profiles = validateProfiles(iss, raw.profiles, groupIds);
  const capNames = new Set<string>();
  for (const prof of Object.values(profiles))
    for (const cap of Object.keys(prof?.caps ?? {})) capNames.add(cap);
  const inputs = validateInputs(iss, raw.inputs);
  const steps = validateSteps(iss, raw.steps, groupIds, capNames);
  const stepIds = new Set(steps.map((s) => s.id));
  const stepSelect = validateStepSelect(iss, raw["step-select"], stepIds);

  if (iss.hasErrors) return { issues: iss.list };

  const def: WorkflowDef = { version: 2, name: name as string, steps };
  if (typeof raw.description === "string") def.description = raw.description;
  if (projectSelection !== undefined) def["project-selection"] = projectSelection;
  if (preflight !== undefined) def.preflight = preflight;
  if (requires !== undefined) def.requires = requires;
  if (raw.tuning !== undefined && raw.tuning !== null) def.tuning = { groups };
  if (raw.profiles !== undefined && raw.profiles !== null) def.profiles = profiles;
  if (raw.inputs !== undefined && raw.inputs !== null) def.inputs = inputs;
  if (stepSelect !== undefined) def["step-select"] = stepSelect;
  return { def, issues: iss.list };
}

/** Locate, load and validate in one call. */
export function loadAndValidate(located: LocatedDef): ValidateResult {
  return validateDef(loadDef(located.path), located.path);
}

// ---- list-defs ---------------------------------------------------------------------------------

export type DefListing = {
  name: string;
  description: string | null;
  source: LocatedDef["source"];
  /** A bundled definition hidden by a user one of the same name. */
  shadowed: boolean;
  path: string;
};

/** Every definition under both roots; user first, folder form before flat within a root. */
export function listDefs(roots: DefRoots): DefListing[] {
  const seen = new Set<string>();
  const items: DefListing[] = [];
  const order: [LocatedDef["source"], string][] = [
    ["user", roots.userRoot],
    ["bundled", roots.bundledRoot],
  ];
  for (const [source, root] of order) {
    if (!isDir(root)) continue;
    const entries: [string, string][] = [];
    const seenInRoot = new Set<string>();
    const children = readdirSync(root).toSorted();
    for (const child of children) {
      const wf = join(root, child, "workflow.yaml");
      if (isDir(join(root, child)) && isFile(wf)) {
        entries.push([child, wf]);
        seenInRoot.add(child);
      }
    }
    for (const child of children) {
      if (!child.endsWith(".yaml")) continue;
      const stem = child.slice(0, -".yaml".length);
      if (seenInRoot.has(stem) || !isFile(join(root, child))) continue;
      entries.push([stem, join(root, child)]);
    }
    for (const [name, path] of entries) {
      let data: unknown;
      try {
        data = loadDef(path);
      } catch (e) {
        items.push({
          name,
          description: `<unreadable: ${(e as Error).message}>`,
          source,
          shadowed: seen.has(name),
          path,
        });
        continue;
      }
      const rec = isRecord(data) ? data : {};
      const desc = typeof rec.description === "string" ? rec.description.trim() : "";
      items.push({
        name: typeof rec.name === "string" && rec.name ? rec.name : name,
        description: desc || null,
        source,
        shadowed: seen.has(name),
        path,
      });
      seen.add(name);
    }
  }
  return items;
}

// ---- installed plugins / probe-requires ------------------------------------------------------

export type PluginsOpts = { pluginsRoot?: string; home?: string };

/**
 * Bare names of the plugins Claude Code reports installed: keys of
 * `<pluginsRoot>/installed_plugins.json` (`<name>@<marketplace>`), else a walk for
 * `.claude-plugin/plugin.json` up to four levels deep (cache layout aware).
 */
export function installedPlugins(opts: PluginsOpts = {}): Set<string> {
  const root = opts.pluginsRoot ?? join(opts.home ?? homedir(), ".claude", "plugins");
  const names = new Set<string>();
  if (!isDir(root)) return names;

  const registry = join(root, "installed_plugins.json");
  if (isFile(registry)) {
    try {
      const data: unknown = JSON.parse(readFileSync(registry, "utf8"));
      const plugins = isRecord(data) && isRecord(data.plugins) ? data.plugins : {};
      for (const key of Object.keys(plugins)) {
        const bare = key.split("@", 1)[0] ?? "";
        if (bare) names.add(bare);
      }
    } catch {
      // malformed registry: fall through to the walk
    }
  }
  if (names.size) return names;

  const cacheLayoutName = (dir: string): string => {
    const rel = dir.startsWith(root) ? dir.slice(root.length).split("/").filter(Boolean) : [];
    return rel.length >= 3 && rel[0] === "cache" ? (rel[2] as string) : basename(dir);
  };
  const walk = (dir: string, depth: number): void => {
    if (depth > 4 || !isDir(dir)) return;
    const pj = join(dir, ".claude-plugin", "plugin.json");
    if (isFile(pj)) {
      let name: string | undefined;
      try {
        const data: unknown = JSON.parse(readFileSync(pj, "utf8"));
        if (isRecord(data) && typeof data.name === "string" && data.name) name = data.name;
      } catch {
        // unreadable plugin.json: recover the name from the path
      }
      names.add(name ?? cacheLayoutName(dir));
      return;
    }
    let children: string[];
    try {
      children = readdirSync(dir);
    } catch {
      return;
    }
    for (const child of children) {
      const full = join(dir, child);
      if (isDir(full)) walk(full, depth + 1);
    }
  };
  let top: string[];
  try {
    top = readdirSync(root);
  } catch {
    return names;
  }
  for (const child of top) {
    const full = join(root, child);
    if (isDir(full)) walk(full, 1);
  }
  return names;
}

export function onPath(
  name: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): boolean {
  for (const dir of (env.PATH ?? "").split(":")) {
    if (!dir) continue;
    try {
      accessSync(join(dir, name), constants.X_OK);
      return true;
    } catch {
      // keep looking
    }
  }
  return false;
}

export type ProbeOpts = {
  installed?: ReadonlySet<string>;
  hasTool?: (name: string) => boolean;
  pluginsRoot?: string;
};

/** Check `requires.plugins` against installed plugins and `requires.tools` against PATH. */
export function probeRequires(
  def: WorkflowDef,
  opts: ProbeOpts = {},
): { ok: boolean; missing: string[] } {
  const req = def.requires ?? {};
  const missing: string[] = [];
  if (req.plugins?.length) {
    const installed =
      opts.installed ?? installedPlugins(opts.pluginsRoot ? { pluginsRoot: opts.pluginsRoot } : {});
    for (const plugin of req.plugins) if (!installed.has(plugin)) missing.push(`plugin:${plugin}`);
  }
  const hasTool = opts.hasTool ?? ((n: string) => onPath(n));
  for (const tool of req.tools ?? []) if (!hasTool(tool)) missing.push(`tool:${tool}`);
  return { ok: missing.length === 0, missing };
}

// ---- inputs ---------------------------------------------------------------------------------------

export type NormalisedInput = InputDef & { prompt: string };

/** Declared inputs with `prompt` filled in (`Value for <name>?`). */
export function listInputs(def: WorkflowDef): NormalisedInput[] {
  return (def.inputs ?? []).map((input) => ({
    ...input,
    prompt: input.prompt ?? `Value for ${input.name}?`,
  }));
}

export type InputCheck =
  | { ok: true; value: string }
  | {
      ok: false;
      reason: "no-match" | "validate" | "bad-extract-regex" | "bad-validate-regex";
      message: string;
    };

/**
 * Apply `extract` (first capture group, else whole match) then `validate` (full match) to a
 * raw value. Empty or missing regexes are skipped, as in Python.
 */
export function validateInput(raw: string, extract?: string, validate?: string): InputCheck {
  let value = raw;
  if (extract) {
    let re: RegExp;
    try {
      re = new RegExp(extract);
    } catch (e) {
      return {
        ok: false,
        reason: "bad-extract-regex",
        message: `INVALID:bad-extract-regex:${(e as Error).message}`,
      };
    }
    const m = re.exec(raw);
    if (!m) return { ok: false, reason: "no-match", message: "INVALID:no-match" };
    value = m.length > 1 ? (m[1] ?? "") : m[0];
  }
  if (validate) {
    let re: RegExp;
    try {
      RegExp(validate);
      re = new RegExp(`^(?:${validate})$`);
    } catch (e) {
      return {
        ok: false,
        reason: "bad-validate-regex",
        message: `INVALID:bad-validate-regex:${(e as Error).message}`,
      };
    }
    if (!re.test(value)) return { ok: false, reason: "validate", message: "INVALID:validate" };
  }
  return { ok: true, value };
}
