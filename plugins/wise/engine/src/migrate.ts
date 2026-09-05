// v1 -> v2 workflow migration (research-ts-engine.md P2, plan delta A16 / M6.4).
// `migrateDef` rewrites a parsed v1 document into a v2 one and records every change as a
// note; `renderDef` serialises the result as YAML with block scalars for prompts and flow
// style for short lists. Output is deterministic: same input, same document and notes.
import { Document, isMap, isScalar, isSeq, visit } from "yaml";
import type { YAMLMap, YAMLSeq } from "yaml";

export type MigrationNoteKind = "rewritten" | "warning" | "manual";
export type MigrationNote = { path: string; kind: MigrationNoteKind; message: string };
export type MigrateResult = { def: unknown; notes: MigrationNote[] };

type Rec = Record<string, unknown>;

function isRecord(v: unknown): v is Rec {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function isStringList(v: unknown): v is string[] {
  return Array.isArray(v) && v.every((x) => typeof x === "string");
}
function show(v: unknown): string {
  return JSON.stringify(v) ?? String(v);
}
function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

class Notes {
  readonly list: MigrationNote[] = [];
  rewritten(path: string, message: string): void {
    this.list.push({ path, kind: "rewritten", message });
  }
  warning(path: string, message: string): void {
    this.list.push({ path, kind: "warning", message });
  }
  manual(path: string, message: string): void {
    this.list.push({ path, kind: "manual", message });
  }
}

// ---- vocabulary ------------------------------------------------------------------------

const V1_STEP_TYPES: ReadonlyMap<string, string> = new Map([
  ["prompt", "agent"],
  ["interactive", "agent"],
  ["supervised-prompt", "agent"],
  ["skill", "agent"],
]);
const V2_STEP_KEYS: ReadonlySet<string> = new Set([
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
  "prompt",
  "skill",
  "schema",
  "outputs",
  "until",
  "run",
  "message",
  "options",
  "allow_text",
  "output",
  "pipeline",
  "items",
  "groups",
  "caps",
  "parallel",
  "reviewers",
]);
const GROUP_KEYS: ReadonlySet<string> = new Set([
  "id",
  "label",
  "description",
  "default",
  "fallback",
  "locked",
  "options",
]);
const INPUT_KEYS: ReadonlySet<string> = new Set([
  "name",
  "prompt",
  "description",
  "optional",
  "default",
  "from-context",
  "validate",
  "extract",
]);
const PREFLIGHT_DROPPED = ["rename_session", "tuning", "step-select"] as const;

/** `until:` regexes that are a plain enum: optional anchors, optional literal framing text
 * around ONE group of literal alternatives (`^ACCESS: (ok|blocked)$`), or a bare
 * alternation (`^a|b|c$`). Anything with a metacharacter stays a regex. */
const ENUM_GROUP_RE =
  /^\^?([A-Za-z0-9_ :=-]*?)\((?:\?:)?([A-Za-z0-9_-]+(?:\|[A-Za-z0-9_-]+)*)\)([A-Za-z0-9_ :=-]*?)\$?$/;
const ENUM_BARE_RE = /^\^?([A-Za-z0-9_-]+(?:\|[A-Za-z0-9_-]+)+)\$?$/;

/** The enum values of a plain-enum `until:` regex, or undefined when it is a real regex. */
export function enumFromUntil(until: string): string[] | undefined {
  const group = ENUM_GROUP_RE.exec(until);
  const alternation = group ? group[2] : ENUM_BARE_RE.exec(until)?.[1];
  if (alternation === undefined) return undefined;
  return [...new Set(alternation.split("|"))];
}

// ---- small rewrites ---------------------------------------------------------------------

/** v1 `"<model> [/ <effort>]"` tuning string. */
function parseTuningString(raw: string): { model: string; effort?: string } | undefined {
  const [model = "", effort] = raw.split("/").map((s) => s.trim());
  if (!model) return undefined;
  return effort ? { model, effort } : { model };
}

function tuningMapping(parsed: { model: string; effort?: string }, withHarness: boolean): Rec {
  const out: Rec = withHarness
    ? { harness: "claude", model: parsed.model }
    : { model: parsed.model };
  if (parsed.effort !== undefined) out.effort = parsed.effort;
  return out;
}

function migrateProjectSelection(value: unknown, notes: Notes): unknown {
  if (value === "prompt") {
    notes.rewritten("project-selection", '"prompt" -> "ask"');
    return "ask";
  }
  if (value === "any") {
    notes.rewritten("project-selection", '"any" -> "none"');
    return "none";
  }
  return value;
}

function migrateRequires(value: unknown, notes: Notes): unknown {
  if (!Array.isArray(value)) return value;
  const plugins: string[] = [];
  for (const [i, entry] of value.entries()) {
    if (isRecord(entry) && typeof entry.plugin === "string") {
      if (!plugins.includes(entry.plugin)) plugins.push(entry.plugin);
    } else if (isRecord(entry) && typeof entry.skill === "string") {
      const owner = entry.skill.split(":", 1)[0] ?? entry.skill;
      if (!plugins.includes(owner)) plugins.push(owner);
    } else notes.warning(`requires[${i}]`, `unrecognised entry ${show(entry)} dropped`);
  }
  notes.rewritten(
    "requires",
    `list of { plugin } / { skill } entries -> { plugins: [${plugins.join(", ")}] }`,
  );
  return { plugins };
}

function migratePreflight(value: unknown, notes: Notes): unknown {
  if (!isRecord(value)) {
    notes.warning("preflight", "not a mapping, kept as is");
    return value;
  }
  const out: Rec = {};
  for (const [key, v] of Object.entries(value)) {
    const p = `preflight.${key}`;
    if ((PREFLIGHT_DROPPED as readonly string[]).includes(key)) {
      notes.rewritten(
        p,
        `dropped \`${key}: ${show(v)}\`; v2 always builds the questionary from tuning / step-select and the harness names sessions`,
      );
      continue;
    }
    if (key === "control-mode") {
      if (v === "wave-sync" || v === "auto-advance" || v === "prompt") {
        out[key] = "interactive";
        notes.rewritten(p, `${show(v)} -> "interactive" (gates pause the run)`);
      } else out[key] = v;
      continue;
    }
    if (key === "worktree") {
      if (v === "prompt") {
        out[key] = "current";
        notes.warning(
          p,
          '"prompt" -> "current"; v2 does not ask, change to "new" if the workflow should edit a throwaway tree',
        );
      } else out[key] = v;
      continue;
    }
    out[key] = v;
    notes.warning(p, `unknown pre-flight key \`${key}\` kept as is`);
  }
  return Object.keys(out).length ? out : undefined;
}

// ---- tuning / profiles / inputs / step-select ---------------------------------------------

type TuningResult = { value: unknown; binding: Map<string, string>; defaults: Map<string, Rec> };

function stepById(steps: readonly unknown[], id: string): Rec | undefined {
  return steps.find((s): s is Rec => isRecord(s) && s.id === id);
}

function migrateGroupDefault(value: unknown, p: string, notes: Notes): Rec | undefined {
  if (typeof value === "string") {
    const parsed = parseTuningString(value);
    if (!parsed) {
      notes.warning(p, `unparseable tuning string ${show(value)} dropped`);
      return undefined;
    }
    const out = tuningMapping(parsed, true);
    notes.rewritten(p, `${show(value)} -> ${show(out)}`);
    return out;
  }
  if (isRecord(value)) {
    if (value.harness !== undefined) return value;
    notes.rewritten(p, "added `harness: claude`");
    return { harness: "claude", ...value };
  }
  notes.warning(p, `unrecognised default ${show(value)} dropped`);
  return undefined;
}

function migrateTuning(raw: unknown, steps: readonly unknown[], notes: Notes): TuningResult {
  const binding = new Map<string, string>();
  const defaults = new Map<string, Rec>();
  if (!isRecord(raw) || !Array.isArray(raw.groups)) {
    notes.warning("tuning", "expected a mapping with `groups:`; kept as is");
    return { value: raw, binding, defaults };
  }
  const groups: unknown[] = [];
  for (const [i, g] of raw.groups.entries()) {
    const p = `tuning.groups[${i}]`;
    if (!isRecord(g)) {
      notes.warning(p, "not a mapping, kept as is");
      groups.push(g);
      continue;
    }
    const id = typeof g.id === "string" ? g.id : "";
    const rest: Rec = {};
    let def: Rec | undefined;
    let bound: string[] = [];
    for (const [key, value] of Object.entries(g)) {
      if (key === "default") def = migrateGroupDefault(value, `${p}.default`, notes);
      else if (key === "steps") {
        if (isStringList(value)) {
          bound = value;
          for (const sid of value) binding.set(sid, id);
          notes.rewritten(
            `${p}.steps`,
            `dropped steps: [${value.join(", ")}]; set \`group: ${id}\` on those steps`,
          );
        } else notes.warning(`${p}.steps`, `unrecognised steps binding ${show(value)} dropped`);
      } else {
        rest[key] = value;
        if (!GROUP_KEYS.has(key))
          notes.warning(`${p}.${key}`, `unknown group key \`${key}\` kept as is`);
      }
    }
    if (def === undefined) {
      const source = bound
        .map((sid) => stepById(steps, sid))
        .find((s) => s && typeof s.model === "string" && s.model !== "inherit");
      if (source) {
        const parsed: { model: string; effort?: string } = { model: source.model as string };
        if (typeof source.effort === "string") parsed.effort = source.effort;
        def = tuningMapping(parsed, true);
        notes.rewritten(`${p}.default`, `derived ${show(def)} from step ${show(source.id)}'s pins`);
      } else {
        def = { harness: "claude" };
        notes.manual(
          `${p}.default`,
          "no default and no bound step pins a model; set model / effort",
        );
      }
    }
    defaults.set(id, def);
    const ordered: Rec = {};
    for (const key of ["id", "label", "description"])
      if (rest[key] !== undefined) ordered[key] = rest[key];
    ordered.default = def;
    for (const [key, value] of Object.entries(rest)) if (!(key in ordered)) ordered[key] = value;
    groups.push(ordered);
  }
  const value: Rec = {};
  for (const [key, v] of Object.entries(raw)) {
    if (key === "groups") value.groups = groups;
    else {
      value[key] = v;
      notes.warning(`tuning.${key}`, `unknown tuning key \`${key}\` kept as is`);
    }
  }
  return { value, binding, defaults };
}

function migrateProfiles(raw: unknown, notes: Notes): unknown {
  if (!isRecord(raw)) {
    notes.warning("profiles", "not a mapping, kept as is");
    return raw;
  }
  const out: Rec = {};
  for (const [level, entryRaw] of Object.entries(raw)) {
    const p = `profiles.${level}`;
    const entry = entryRaw ?? {};
    if (!isRecord(entry)) {
      out[level] = entryRaw;
      notes.warning(p, "not a mapping, kept as is");
      continue;
    }
    const prof: Rec = {};
    for (const [key, value] of Object.entries(entry)) {
      switch (key) {
        case "tuning": {
          if (!isRecord(value)) {
            prof.tuning = value;
            notes.warning(`${p}.tuning`, "not a mapping, kept as is");
            break;
          }
          const t: Rec = {};
          for (const [gid, v] of Object.entries(value)) {
            const tp = `${p}.tuning.${gid}`;
            if (v === "default") {
              notes.rewritten(tp, 'dropped "default" (omitting the group keeps its default)');
              continue;
            }
            if (typeof v === "string") {
              const parsed = parseTuningString(v);
              if (parsed) {
                t[gid] = tuningMapping(parsed, false);
                notes.rewritten(tp, `${show(v)} -> ${show(t[gid])}`);
              } else notes.warning(tp, `unparseable tuning string ${show(v)} dropped`);
              continue;
            }
            t[gid] = v;
          }
          if (Object.keys(t).length) prof.tuning = t;
          break;
        }
        case "step-preset":
        case "skip":
          notes.warning(
            `${p}.${key}`,
            `dropped ${key}: ${show(value)}; v2 has no presets, optional steps are chosen in the step-select question`,
          );
          break;
        case "team-mode":
          notes.rewritten(`${p}.team-mode`, "dropped team-mode; v2 has no agent teams");
          break;
        case "caps":
        case "description":
          prof[key] = value;
          break;
        default:
          prof[key] = value;
          notes.warning(`${p}.${key}`, `unknown profile key \`${key}\` kept as is`);
      }
    }
    out[level] = prof;
  }
  return out;
}

function contextPathFor(name: string): string | undefined {
  if (/^ticket(_ids?|s)?$/.test(name)) return "ticket[].ref";
  if (/^(config_prompt|guidance)$/.test(name)) return "guidance";
  return undefined;
}

function migrateInputs(raw: unknown, notes: Notes): unknown {
  if (!Array.isArray(raw)) {
    notes.warning("inputs", "not a list, kept as is");
    return raw;
  }
  return raw.map((entry, i) => {
    const p = `inputs[${i}]`;
    if (!isRecord(entry)) {
      notes.warning(p, "not a mapping, kept as is");
      return entry;
    }
    const name = typeof entry.name === "string" ? entry.name : "";
    const options = Array.isArray(entry.options)
      ? entry.options
          .map((o) =>
            isRecord(o)
              ? { value: String(o.value ?? ""), label: typeof o.label === "string" ? o.label : "" }
              : { value: String(o), label: "" },
          )
          .filter((o) => o.value)
      : undefined;
    const out: Rec = {};
    for (const [key, value] of Object.entries(entry)) {
      if (key === "options") continue;
      if (key === "prompt" && options && typeof value === "string") {
        const menu = options.map((o) => (o.label ? `${o.value}: ${o.label}` : o.value)).join(" | ");
        out.prompt = `${value} (${menu})`;
        continue;
      }
      out[key] = value;
      if (!INPUT_KEYS.has(key))
        notes.warning(`${p}.${key}`, `unknown input key \`${key}\` kept as is`);
    }
    if (options) {
      if (out.validate === undefined) {
        out.validate = `^(${options.map((o) => escapeRegex(o.value)).join("|")})$`;
      }
      notes.rewritten(
        `${p}.options`,
        `choice input ${show(name)} -> validate ${show(out.validate)}; option labels folded into the prompt`,
      );
    }
    if (out["from-context"] === undefined) {
      const fc = contextPathFor(name);
      if (fc) {
        out["from-context"] = fc;
        notes.rewritten(
          `${p}.from-context`,
          `added from-context: ${fc} (pre-filled from the run context)`,
        );
      }
    }
    return out;
  });
}

type StepSelectResult = { value: unknown; labels: Map<string, string> };

function migrateStepSelect(raw: unknown, notes: Notes): StepSelectResult {
  const labels = new Map<string, string>();
  if (!isRecord(raw)) {
    notes.warning("step-select", "not a mapping, kept as is");
    return { value: raw, labels };
  }
  const out: Rec = {};
  for (const [key, value] of Object.entries(raw)) {
    switch (key) {
      case "prompt":
        out.prompt = value;
        break;
      case "optional": {
        if (!Array.isArray(value)) {
          out.optional = value;
          notes.warning("step-select.optional", "not a list, kept as is");
          break;
        }
        const ids: string[] = [];
        for (const [i, entry] of value.entries()) {
          const p = `step-select.optional[${i}]`;
          if (typeof entry === "string") {
            if (!ids.includes(entry)) ids.push(entry);
            continue;
          }
          if (!isRecord(entry)) {
            notes.warning(p, `unrecognised entry ${show(entry)} dropped`);
            continue;
          }
          const id = typeof entry.id === "string" ? entry.id : "";
          const steps = isStringList(entry.steps) && entry.steps.length ? entry.steps : [id];
          for (const sid of steps) {
            if (!ids.includes(sid)) ids.push(sid);
            if (typeof entry.label === "string" && !labels.has(sid)) labels.set(sid, entry.label);
          }
          notes.rewritten(
            p,
            `entry ${show(id)} -> step ids [${steps.join(", ")}]; its label becomes those steps' description`,
          );
          if (steps.length > 1) {
            notes.manual(
              p,
              `entry ${show(id)} covered ${steps.length} steps; v2 offers each step separately, review whether all of them should be selectable`,
            );
          }
        }
        out.optional = ids;
        break;
      }
      case "presets":
        notes.warning(
          "step-select.presets",
          "dropped presets; v2 asks one multi-select over `optional:`",
        );
        break;
      default:
        out[key] = value;
        notes.warning(`step-select.${key}`, `unknown step-select key \`${key}\` kept as is`);
    }
  }
  return { value: out, labels };
}

// ---- steps ------------------------------------------------------------------------------------

type StepCtx = {
  binding: Map<string, string>;
  defaults: Map<string, Rec>;
  labels: Map<string, string>;
};

function roleName(v: unknown): string | undefined {
  if (typeof v !== "string") return undefined;
  const role = v.trim().replace(/^wise:/, "");
  return role && role !== "auto" && role !== "off" ? role : undefined;
}

function rolePrefix(lead: string, others: readonly string[]): string {
  const files = [lead, ...others].map((r, i) =>
    i === 0 ? `\${CLAUDE_PLUGIN_ROOT}/agents/${r}.md` : `${r}.md`,
  );
  if (others.length === 0) return `Act as the wise \`${lead}\` agent (see ${files[0]}).`;
  const lenses = others.map((r) => `\`${r}\``).join(", ");
  return `Act as the wise \`${lead}\` agent leading this step and cover the ${lenses} ${others.length > 1 ? "lenses" : "lens"} too (see ${files.join(", ")}).`;
}

/** The "Act as ..." line for a v1 `agent:` binding, or undefined when nothing is prepended. */
function agentPrefix(agent: unknown, p: string, notes: Notes): string | undefined {
  if (agent === undefined) return undefined;
  if (agent === "off" || agent === false || agent === "auto") {
    notes.rewritten(`${p}.agent`, `dropped \`agent: ${String(agent)}\`; v2 has no roster routing`);
    return undefined;
  }
  if (typeof agent === "string") {
    const role = roleName(agent);
    if (!role) {
      notes.warning(`${p}.agent`, `unrecognised agent ${show(agent)} dropped`);
      return undefined;
    }
    notes.rewritten(
      `${p}.agent`,
      `role ${show(role)} folded into the prompt ("Act as the wise ${role} agent")`,
    );
    return rolePrefix(role, []);
  }
  if (Array.isArray(agent)) {
    const members = agent
      .map((m) =>
        isRecord(m)
          ? { role: roleName(m.role), lead: m.lead === true }
          : { role: roleName(m), lead: false },
      )
      .filter((m): m is { role: string; lead: boolean } => m.role !== undefined);
    const lead = members.find((m) => m.lead) ?? members[0];
    if (!lead) {
      notes.warning(`${p}.agent`, "team with no recognisable roles dropped");
      return undefined;
    }
    const others = members.filter((m) => m !== lead).map((m) => m.role);
    notes.manual(
      `${p}.agent`,
      `team [${members.map((m) => m.role).join(", ")}] folded into one agent led by ${lead.role}; v2 has no agent teams, review the prompt (member model / effort overrides dropped)`,
    );
    return rolePrefix(lead.role, others);
  }
  notes.warning(`${p}.agent`, `unrecognised agent binding ${show(agent)} dropped`);
  return undefined;
}

type EnumInfo = { name: string; values: string[] };

function schemaHint(outputs: readonly string[]): string {
  const props = outputs.length
    ? outputs.map((o) => `${o}: { type: string }`).join(", ")
    : "<name>: { type: string }";
  return `schema: { type: object, properties: { ${props} }, required: [${outputs.join(", ")}] } (keep outputs: as the names to copy)`;
}

function enumInfo(step: Rec, p: string, id: string, notes: Notes): EnumInfo | undefined {
  if (typeof step.until !== "string") return undefined;
  const values = enumFromUntil(step.until);
  const outputs = isStringList(step.outputs) ? step.outputs : [];
  if (!values || outputs.length > 1) {
    notes.warning(
      `${p}.until`,
      `kept \`until:\` ${show(step.until)} (deprecated regex capture, not a plain enum); replace it with ${schemaHint(outputs)} and rewrite the prompt to return the structured result`,
    );
    return undefined;
  }
  const name = outputs[0] ?? id.replaceAll("-", "_");
  notes.rewritten(
    `${p}.until`,
    `enum regex ${show(step.until)} -> schema { ${name}: enum [${values.join(", ")}] } + outputs [${name}]; the prompt's verdict-line wording is now stale`,
  );
  return { name, values };
}

function enumSchema(info: EnumInfo): Rec {
  return {
    type: "object",
    properties: { [info.name]: { type: "string", enum: info.values } },
    required: [info.name],
    additionalProperties: false,
  };
}

function composePrompt(
  prompt: string,
  prefix: string | undefined,
  info: EnumInfo | undefined,
): string {
  let out = prompt;
  if (prefix !== undefined) out = `${prefix}\n\n${out}`;
  if (info) {
    const nl = out.endsWith("\n") ? "\n" : "";
    out = `${out.trimEnd()}\n\nReturn the field directly as the structured result (no wrapping, no JSON-in-a-string): \`${info.name}\` = one of ${info.values.join(" | ")}.${nl}`;
  }
  return out;
}

function isEmptyPayload(v: unknown): boolean {
  if (v === undefined || v === null || v === "") return true;
  if (Array.isArray(v)) return v.length === 0;
  return isRecord(v) && Object.keys(v).length === 0;
}

function migrateStep(step: Rec, i: number, ctx: StepCtx, notes: Notes): Rec {
  const p = `steps[${i}]`;
  const id = typeof step.id === "string" ? step.id : "";
  const v1Type = typeof step.type === "string" ? step.type : "";
  const type = V1_STEP_TYPES.get(v1Type) ?? v1Type;
  const isAgent = type === "agent";
  const group = ctx.binding.get(id);
  const groupDefault = group === undefined ? undefined : ctx.defaults.get(group);

  const prefix = isAgent ? agentPrefix(step.agent, p, notes) : undefined;
  const info = isAgent ? enumInfo(step, p, id, notes) : undefined;
  const skill = typeof step.skill === "string" ? step.skill.trim().replace(/^\//, "") : undefined;
  const payloadPrompt =
    skill !== undefined && !isEmptyPayload(step.payload)
      ? `Run /${skill} with: ${typeof step.payload === "string" ? step.payload : JSON.stringify(step.payload)}`
      : undefined;

  let run =
    typeof step.command === "string"
      ? step.command
      : typeof step.run === "string"
        ? step.run
        : undefined;
  if (run !== undefined && typeof step.cwd === "string" && step.cwd.trim() !== "{{project.path}}") {
    run = `cd "${step.cwd}" || exit 1\n${run}`;
  }

  const out: Rec = {};
  for (const [key, value] of Object.entries(step)) {
    const kp = `${p}.${key}`;
    switch (key) {
      case "type":
        out.type = type;
        if (type !== v1Type) notes.rewritten(kp, `type ${show(v1Type)} -> "agent"`);
        if (group !== undefined && step.group === undefined) {
          out.group = group;
          notes.rewritten(
            `${p}.group`,
            `bound to tuning group ${show(group)} (was the group's steps: list)`,
          );
        }
        break;
      case "prompt":
        out.prompt =
          isAgent && typeof value === "string" ? composePrompt(value, prefix, info) : value;
        break;
      case "skill":
        if (payloadPrompt !== undefined) {
          out.prompt = payloadPrompt;
          out.harness = "claude";
          notes.rewritten(
            kp,
            `skill ${show(skill)} + payload -> prompt ${show(payloadPrompt)} (harness claude)`,
          );
        } else {
          out.skill = skill;
          notes.rewritten(
            kp,
            `skill ${show(skill)} kept as \`skill:\` sugar (emits "Run /${skill}", harness claude)`,
          );
        }
        break;
      case "payload":
        if (isEmptyPayload(value)) notes.rewritten(kp, "dropped empty payload");
        break;
      case "until":
        if (info) {
          out.schema = enumSchema(info);
          if (!isStringList(step.outputs)) out.outputs = [info.name];
        } else out.until = value;
        break;
      case "max_iterations":
        if (
          typeof value === "number" &&
          Number.isInteger(value) &&
          value >= 1 &&
          value <= 10 &&
          step.max_turns === undefined
        ) {
          out.max_turns = value;
          notes.rewritten(
            kp,
            `max_iterations ${value} -> max_turns ${value} (turns of one child, not whole-step retries; raise it when the step uses tools)`,
          );
        } else
          notes.warning(
            kp,
            `dropped max_iterations ${show(value)}; set \`max_turns:\` if the step needs a turn cap`,
          );
        break;
      case "agent":
        break;
      case "command":
        out.run = run;
        notes.rewritten(kp, "command -> run");
        break;
      case "run":
        out.run = run;
        break;
      case "success": {
        const s = isRecord(value) ? value : {};
        const lost = Object.keys(s).filter((k) => !(k === "exit_code" && s[k] === 0));
        if (lost.length) {
          notes.warning(
            kp,
            `dropped success: { ${lost.join(", ")} }; v2 succeeds on exit code 0, assert the output inside run: or through outputs:`,
          );
        } else notes.rewritten(kp, "dropped success (exit code 0 is the v2 success)");
        break;
      }
      case "cwd":
        if (typeof value === "string" && value.trim() !== "{{project.path}}") {
          notes.rewritten(kp, `cwd ${show(value)} -> \`cd\` at the top of run:`);
        } else notes.rewritten(kp, "dropped cwd (steps run in the project path)");
        break;
      case "question":
        out.message = value;
        notes.rewritten(kp, "question -> message");
        break;
      case "header":
        notes.rewritten(kp, "dropped header; the harness renders the gate");
        break;
      case "skip_label":
      case "confirm_label":
      case "confirm_value": {
        if (out.options !== undefined) break;
        const options: string[] = [];
        if (typeof step.skip_label === "string") options.push(step.skip_label);
        if (typeof step.confirm_label === "string") options.push(step.confirm_label);
        if (options.length) out.options = options;
        if (step.confirm_label === undefined) out.allow_text = true;
        notes.rewritten(
          kp,
          `skip_label / confirm_label -> options: ${show(options)}${step.confirm_label === undefined ? " + allow_text: true" : ""}`,
        );
        if (typeof step.skip_label === "string") {
          const output = typeof step.output === "string" ? step.output : "<output>";
          notes.manual(
            kp,
            `skip now records the option text, not ''; update \`when:\` guards comparing ${output} to '' (and any confirm_value match)`,
          );
        }
        break;
      }
      case "when":
        if (Array.isArray(value)) {
          out.when = value.map(String).join(" && ");
          notes.rewritten(kp, `list of conditions -> ${show(out.when)}`);
        } else out.when = value;
        break;
      case "model":
      case "effort":
      case "harness":
        if (groupDefault) {
          const differs = groupDefault[key] !== value;
          if (differs) {
            notes.warning(
              kp,
              `dropped ${key}: ${show(value)}; the tuning group ${show(group)} carries ${show(groupDefault[key])}`,
            );
          } else
            notes.rewritten(
              kp,
              `dropped ${key}: ${show(value)}; carried by tuning group ${show(group)}`,
            );
        } else if (key === "model" && value === "inherit") {
          notes.rewritten(kp, 'dropped model: "inherit" (the default)');
        } else out[key] = value;
        break;
      case "surface":
        notes.warning(
          kp,
          "dropped surface; v2 has no chat surfacing, the step's output lands in the run log",
        );
        break;
      default:
        out[key] = value;
        if (!V2_STEP_KEYS.has(key)) notes.warning(kp, `unknown step key \`${key}\` kept as is`);
    }
  }
  const label = ctx.labels.get(id);
  if (label !== undefined && out.description === undefined) {
    out.description = label;
    notes.rewritten(`${p}.description`, `step-select label ${show(label)} -> description`);
  }
  if (v1Type === "interactive") {
    notes.manual(
      `${p}.type`,
      "`interactive` ran inline in the conductor; it is now an isolated agent child: ask the user through the `wise_ask` tool, read context with `wise_context`, add `mode: full-access` for unattended git",
    );
  } else if (v1Type === "supervised-prompt") {
    notes.manual(
      `${p}.type`,
      "`supervised-prompt` hang protection is now the adapter's `timeout:` / `stale_after:`; set them on the step",
    );
  }
  return out;
}

// ---- top level ----------------------------------------------------------------------------------

/**
 * Rewrite a parsed v1 workflow document as v2. `def` is a plain object in the source key
 * order (`version` first); `notes` lists every rewrite, every dropped construct, and the
 * places a human still edits. A v2 document comes back unchanged with a single note.
 */
export function migrateDef(raw: unknown, path: string): MigrateResult {
  const notes = new Notes();
  if (!isRecord(raw)) {
    notes.warning("", `${path}: expected a YAML mapping at the top level; nothing migrated`);
    return { def: raw, notes: notes.list };
  }
  if (raw.version === 2) {
    notes.warning("version", "already version 2; nothing to migrate");
    return { def: raw, notes: notes.list };
  }
  const out: Rec = { version: 2 };
  if (raw.version === undefined) notes.rewritten("version", "added `version: 2`");
  else if (raw.version === 1) notes.rewritten("version", "version 1 -> 2");
  else notes.warning("version", `unsupported version ${show(raw.version)} treated as v1`);

  const steps = Array.isArray(raw.steps) ? raw.steps : [];
  const tuning = raw.tuning === undefined ? undefined : migrateTuning(raw.tuning, steps, notes);
  const stepSelect =
    raw["step-select"] === undefined ? undefined : migrateStepSelect(raw["step-select"], notes);
  const ctx: StepCtx = {
    binding: tuning?.binding ?? new Map(),
    defaults: tuning?.defaults ?? new Map(),
    labels: stepSelect?.labels ?? new Map(),
  };

  for (const [key, value] of Object.entries(raw)) {
    switch (key) {
      case "version":
        break;
      case "name":
      case "description":
      case "author":
        out[key] = value;
        break;
      case "project-selection":
        out[key] = migrateProjectSelection(value, notes);
        break;
      case "agents":
        notes.rewritten(
          "agents",
          `dropped workflow-level \`agents: ${show(value)}\`; v2 has no roster routing, each agent step carries its own prompt`,
        );
        break;
      case "requires":
        out.requires = migrateRequires(value, notes);
        break;
      case "preflight": {
        const pf = migratePreflight(value, notes);
        if (pf !== undefined) out.preflight = pf;
        break;
      }
      case "tuning":
        out.tuning = tuning?.value;
        break;
      case "step-select":
        out["step-select"] = stepSelect?.value;
        break;
      case "profiles":
        out.profiles = migrateProfiles(value, notes);
        break;
      case "inputs":
        out.inputs = migrateInputs(value, notes);
        break;
      case "steps":
        if (Array.isArray(value)) {
          out.steps = value.map((s, i) => {
            if (isRecord(s)) return migrateStep(s, i, ctx, notes);
            notes.warning(`steps[${i}]`, "not a mapping, kept as is");
            return s;
          });
        } else {
          out.steps = value;
          notes.warning("steps", "not a list, kept as is");
        }
        break;
      default:
        out[key] = value;
        notes.warning(key, `unknown top-level key \`${key}\` kept as is`);
    }
  }
  return { def: out, notes: notes.list };
}

// ---- rendering ----------------------------------------------------------------------------------

const FLOW_SEQ_KEYS: ReadonlySet<string> = new Set([
  "outputs",
  "required",
  "enum",
  "allowed_tools",
  "options",
  "fallback",
  "plugins",
  "tools",
  "optional",
  "caps",
  "reviewers",
]);
const FLOW_MAP_KEYS: ReadonlySet<string> = new Set(["default", "value"]);
/** Words a YAML 1.1 reader would take for booleans / null; quoted so both readers agree. */
const YAML11_WORDS = /^(?:y|n|yes|no|on|off|true|false|null|~)$/i;
/** Matches the repo's print width; folds long one-line strings and wraps wider flow lists. */
const LINE_WIDTH = 100;

/** Approximate flow rendering of a node made of scalars (and flow-able seqs), else undefined. */
function flowText(node: unknown): string | undefined {
  if (isScalar(node)) return String(node.value);
  if (isSeq(node)) {
    const items = node.items.map(flowText);
    return items.every((s) => s !== undefined) ? `[${items.join(", ")}]` : undefined;
  }
  if (isMap(node)) {
    const items = node.items.map((p) => {
      const v = flowText(p.value);
      return v === undefined || !isScalar(p.key) ? undefined : `${String(p.key.value)}: ${v}`;
    });
    return items.every((s) => s !== undefined) ? `{${items.join(", ")}}` : undefined;
  }
  return undefined;
}

/** Flow style when the whole `key: {...}` line fits LINE_WIDTH at its indent. */
function flowIfFits(node: YAMLMap | YAMLSeq, key: string, indent: number): void {
  const text = flowText(node);
  if (text !== undefined && indent + key.length + 2 + text.length <= LINE_WIDTH) node.flow = true;
}

function isScalarMap(node: unknown): node is YAMLMap {
  return isMap(node) && node.items.every((p) => isScalar(p.value));
}

/** Serialise a workflow document: block scalars for multi-line strings, folded blocks for
 * long one-line strings, flow style for the short lists and mappings the hand-written v2
 * workflows use (when they fit the line), regexes single-quoted. */
export function renderDef(def: unknown): string {
  const doc = new Document(def);
  visit(doc, {
    Pair(_, pair, path) {
      const key = isScalar(pair.key) ? String(pair.key.value) : "";
      const value = pair.value;
      const indent = Math.max(0, path.filter((n) => isMap(n) || isSeq(n)).length - 1) * 2;
      if (isSeq(value) && value.items.every((item) => isScalar(item))) {
        if (FLOW_SEQ_KEYS.has(key) || (key === "depends_on" && value.items.length <= 3)) {
          flowIfFits(value, key, indent);
        }
      } else if (isMap(value)) {
        if (FLOW_MAP_KEYS.has(key) && isScalarMap(value)) flowIfFits(value, key, indent);
        else if (key === "properties") {
          for (const prop of value.items) {
            if (isMap(prop.value) && isScalar(prop.key)) {
              flowIfFits(prop.value, String(prop.key.value), indent + 2);
            }
          }
        } else if (
          key === "tuning" &&
          value.items.length &&
          value.items.every((p) => isScalarMap(p.value))
        ) {
          for (const prop of value.items) {
            if (isScalar(prop.key))
              flowIfFits(prop.value as YAMLMap, String(prop.key.value), indent + 2);
          }
        }
      }
    },
    Scalar(_, node) {
      if (typeof node.value !== "string") return;
      if (YAML11_WORDS.test(node.value)) node.type = "QUOTE_DOUBLE";
      else if (node.value.includes("\n")) node.type = "BLOCK_LITERAL";
      else if (
        node.value.length > LINE_WIDTH &&
        node.value.includes(" ") &&
        !/^\s|\s$|\s\s/.test(node.value)
      ) {
        node.type = "BLOCK_FOLDED";
      }
    },
  });
  return doc.toString({ lineWidth: LINE_WIDTH, singleQuote: true, flowCollectionPadding: false });
}
