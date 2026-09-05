// Model / effort / team resolution. 1:1 port of the resolve section of
// plugins/wise/scripts/workflows.py: model family, capability clamp, policy
// ceilings (+ WISE_EFFORT_CEILING override), retired ids, the low-profile Opus
// rule, the SDLC roster and `resolve-team`. Pure functions; the filesystem is
// touched only through paths passed in (roster dir, definition path).
import { readdirSync, readFileSync } from "node:fs";
import { basename, join } from "node:path";
import { parse as parseYaml } from "yaml";
import type { Effort, Harness, Resolved } from "./types.ts";
import { EFFORTS, PROFILE_LEVELS } from "./types.ts";
import { PLUGIN_ROOT } from "./version.ts";

// ---- constants ---------------------------------------------------------------

/** wise's per-step `effort:` scale, low to high. */
export const EFFORT_ORDER: readonly Effort[] = EFFORTS;

export type EffortSupport = Readonly<Record<string, ReadonlySet<string>>>;

/**
 * Which effort levels each Claude model family accepts (capability). A family
 * not listed is unrestricted; an empty set means no effort control at all.
 */
export const MODEL_EFFORT_SUPPORT: EffortSupport = {
  opus: new Set(EFFORTS),
  fable: new Set(EFFORTS),
  sonnet: new Set(EFFORTS),
  haiku: new Set(),
};

/**
 * Policy effort ceilings: the highest effort wise will request from a model.
 * Keyed per model (not family). Exact match first, then the longest `claude-`
 * key the model is a dated snapshot of (`<base>-YYYYMMDD`).
 */
export const MODEL_EFFORT_CEILING: Readonly<Record<string, Effort>> = {
  opus: "high",
  "claude-opus-5": "high",
  "claude-opus-4-8": "xhigh",
};

/** Under the `low` profile every Opus-family pin resolves to this model. */
export const LOW_PROFILE_OPUS_MODEL = "claude-opus-4-8";

/** One-hop fallback per alias family when a pinned model is unavailable. */
export const MODEL_TIER_NEXT: Readonly<Record<string, string>> = {
  fable: "opus",
  opus: "sonnet",
  sonnet: "haiku",
  haiku: "sonnet",
};

/** Known-retired / deprecated full ids -> [replacement alias, state]. */
export const RETIRED_MODELS: Readonly<Record<string, readonly [string, string]>> = {
  "claude-3-opus-20240229": ["opus", "retired"],
  "claude-3-sonnet-20240229": ["sonnet", "retired"],
  "claude-3-5-sonnet-20240620": ["sonnet", "retired"],
  "claude-3-5-sonnet-20241022": ["sonnet", "retired"],
  "claude-3-7-sonnet-20250219": ["sonnet", "retired"],
  "claude-3-haiku-20240307": ["haiku", "retired"],
  "claude-3-5-haiku-20241022": ["haiku", "retired"],
  "claude-opus-4-20250514": ["opus", "deprecated"],
  "claude-sonnet-4-20250514": ["sonnet", "deprecated"],
  "claude-opus-4-1-20250805": ["opus", "deprecated"],
};

/** Bundled SDLC roster: `plugins/wise/agents/*.md`. */
export const DEFAULT_ROSTER_DIR: string = join(PLUGIN_ROOT, "agents");

const FAMILIES = ["opus", "sonnet", "haiku", "fable"] as const;

// ---- types -------------------------------------------------------------------

export type Env = Readonly<Record<string, string | undefined>>;

export type ResolveOptions = {
  /** Source of `WISE_EFFORT_CEILING`; defaults to `process.env`. */
  env?: Env;
  /** Capability table; defaults to `MODEL_EFFORT_SUPPORT`. */
  effortSupport?: EffortSupport;
  /** Harness recorded on the resolution; defaults to `claude`. */
  harness?: Harness;
};

/** `Resolved` plus the availability hints. `effort: ""` means no effort (Python `None`). */
export type ModelResolution = Resolved & {
  fell_back: boolean;
  next_fallback?: string;
};

export type RosterAgent = {
  name: string;
  description: string | null;
  tools: string[];
  model: string;
  effort: string | null;
};

export type TeamMode = "unset" | "off" | "auto" | "single" | "team";

export type TeamMember = ModelResolution & { role: string; lead: boolean };

export type TeamResult = {
  mode: TeamMode;
  lead: string | null;
  members: TeamMember[];
  errors: string[];
  /** Present only after a `solo` collapse. */
  collapsed?: { from: number; dropped: string[] };
};

export type TeamOptions = ResolveOptions & {
  /** `--model` run-level override; wins over member and step pins. */
  modelOverride?: string;
  /** `--effort` run-level override. */
  effortOverride?: string;
  /** `--team-mode`: `full` (default) or `solo` (collapse a team to its lead). */
  teamMode?: string;
  /** `--profile`: the run's budget level; `low` applies the Opus rule. */
  profile?: string;
  rosterDir?: string;
};

/** Thrown by the `cmd*` entry points where the Python CLI exited non-zero. */
export class ResolveError extends Error {
  exitCode: number;
  constructor(message: string, exitCode: number) {
    super(message);
    this.name = "ResolveError";
    this.exitCode = exitCode;
  }
}

// ---- helpers -----------------------------------------------------------------

function isEffort(value: string): value is Effort {
  return (EFFORTS as readonly string[]).includes(value);
}

function own<T>(table: Readonly<Record<string, T>>, key: string): T | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

// ---- model family / effort clamps ---------------------------------------------

/** Normalise a model id/alias to a family key, `inherit`, or `""` if unknown. */
export function modelFamily(model: string): string {
  const m = model.trim().toLowerCase();
  if (!m || m === "inherit") return "inherit";
  if ((FAMILIES as readonly string[]).includes(m)) return m;
  for (const fam of FAMILIES) {
    if (m.includes(`claude-${fam}`) || m.startsWith(fam)) return fam;
  }
  return "";
}

/**
 * Clamp `effort` to what `family` supports -> [effort, changed]. Unknown or
 * `inherit` family leaves it untouched; a family with no effort control drops
 * it (returns `""`). A value outside EFFORT_ORDER passes through unchanged.
 */
export function downmapEffort(
  family: string,
  effort: string,
  support: EffortSupport = MODEL_EFFORT_SUPPORT,
): [string, boolean] {
  const eff = effort.trim().toLowerCase();
  if (!eff) return ["", false];
  const supported = own(support, family);
  if (supported === undefined) return [eff, false];
  if (supported.size === 0) return ["", true];
  if (supported.has(eff)) return [eff, false];
  if (!isEffort(eff)) return [eff, false];
  for (let j = EFFORT_ORDER.indexOf(eff); j >= 0; j--) {
    const candidate = EFFORT_ORDER[j];
    if (candidate !== undefined && supported.has(candidate)) return [candidate, true];
  }
  return ["", true];
}

/**
 * MODEL_EFFORT_CEILING with the `WISE_EFFORT_CEILING` override applied.
 * Value shape: `<model>=<level>,...`; `<model>=off` drops one entry, a bare
 * `off` disables every ceiling. Junk pairs are ignored. Returns a fresh Map.
 */
export function effortCeilings(env: Env = process.env): Map<string, Effort> {
  const table = new Map<string, Effort>(Object.entries(MODEL_EFFORT_CEILING));
  const raw = (env["WISE_EFFORT_CEILING"] ?? "").trim();
  if (!raw) return table;
  if (raw.toLowerCase() === "off") return new Map();
  for (const pair of raw.split(",")) {
    const at = pair.indexOf("=");
    if (at === -1) continue;
    const key = pair.slice(0, at).trim().toLowerCase();
    const level = pair
      .slice(at + 1)
      .trim()
      .toLowerCase();
    if (!key) continue;
    if (level === "off" || level === "none" || level === "") {
      table.delete(key);
    } else if (isEffort(level)) {
      table.set(key, level);
    }
  }
  return table;
}

/** Whether `model` is `key` plus a `-YYYYMMDD` suffix and nothing else. */
export function isSnapshotOf(model: string, key: string): boolean {
  if (!model.startsWith(`${key}-`)) return false;
  const suffix = model.slice(key.length + 1);
  return /^[0-9]{8}$/.test(suffix);
}

/** The policy ceiling for `model`, or `""` when it has none. */
export function effortCeiling(model: string, env: Env = process.env): string {
  const m = model.trim().toLowerCase();
  if (!m || m === "inherit") return "";
  const table = effortCeilings(env);
  const exact = table.get(m);
  if (exact !== undefined) return exact;
  let best = "";
  let ceiling = "";
  for (const [key, level] of table) {
    if (key.startsWith("claude-") && key.length > best.length && isSnapshotOf(m, key)) {
      best = key;
      ceiling = level;
    }
  }
  return ceiling;
}

/** Clamp `effort` to `model`'s policy ceiling -> [effort, changed]. */
export function capEffort(
  model: string,
  effort: string,
  env: Env = process.env,
): [string, boolean] {
  const eff = effort.trim().toLowerCase();
  const ceiling = effortCeiling(model, env);
  if (!eff || !isEffort(eff) || !isEffort(ceiling)) return [effort, false];
  if (EFFORT_ORDER.indexOf(eff) <= EFFORT_ORDER.indexOf(ceiling)) return [effort, false];
  return [ceiling, true];
}

/**
 * The model the low-profile Opus rule dispatches for `model`, or `""`.
 * Non-empty only for an Opus-family pin that is not already Opus 4.8.
 */
export function lowProfileModel(model: string, family: string): string {
  if (family !== "opus") return "";
  const m = model.trim().toLowerCase();
  if (m === LOW_PROFILE_OPUS_MODEL || isSnapshotOf(m, LOW_PROFILE_OPUS_MODEL)) return "";
  return LOW_PROFILE_OPUS_MODEL;
}

/**
 * Resolve a pinned model+effort: retired-id substitution, the low-profile
 * Opus rule (when `profile` is `low`), the capability clamp, then the policy
 * ceiling. `reason` is set only when something changed.
 */
export function resolveModelDict(
  pinned: string,
  effort = "",
  profile = "",
  opts: ResolveOptions = {},
): ModelResolution {
  const env = opts.env ?? process.env;
  const support = opts.effortSupport ?? MODEL_EFFORT_SUPPORT;
  const pin = pinned.trim();
  const eff = effort.trim();
  const level = profile.trim().toLowerCase();
  const reasons: string[] = [];
  let model = pin || "inherit";
  let fellBack = false;

  const retired = own(RETIRED_MODELS, pin);
  if (retired) {
    const [repl, state] = retired;
    reasons.push(`${pin} is ${state}; using ${repl}`);
    model = repl;
    fellBack = true;
  }

  const family = modelFamily(model);
  if (level === "low") {
    const low = lowProfileModel(model, family);
    if (low) {
      reasons.push(`low profile: ${model}→${low} (Opus 5 is never used at low)`);
      model = low;
    }
  }
  let [effOut, changed] = downmapEffort(family, eff, support);
  if (changed) {
    if (!effOut) {
      reasons.push(`${model} has no effort control; effort '${eff}' dropped`);
    } else {
      reasons.push(`effort ${eff}→${effOut} (${model} capability ceiling)`);
    }
  }

  if (effOut) {
    const [capped, lowered] = capEffort(model, effOut, env);
    if (lowered) {
      reasons.push(`effort ${effOut}→${capped} (${model} policy ceiling)`);
      effOut = capped;
    }
  }

  const out: ModelResolution = {
    harness: opts.harness ?? "claude",
    model,
    // A non-standard value passes through unchanged, as in Python; the v2
    // compiler rejects those upstream, so this cast never widens in practice.
    effort: effOut as Effort | "",
    fell_back: fellBack,
  };
  if (reasons.length > 0) out.reason = reasons.join("; ");
  const next = own(MODEL_TIER_NEXT, family);
  if (next !== undefined) out.next_fallback = next;
  return out;
}

/** `resolve-model` entry point. Throws `ResolveError(2)` on an unknown profile. */
export function cmdResolveModel(
  pinned: string,
  effort = "",
  profile = "",
  opts: ResolveOptions = {},
): ModelResolution {
  const level = profile.trim().toLowerCase();
  if (level && !(PROFILE_LEVELS as readonly string[]).includes(level)) {
    throw new ResolveError(`INVALID:profile-level:${level}`, 2);
  }
  return resolveModelDict(pinned, effort, level, opts);
}

/**
 * P6 effort mapping: what the harness CLI receives for a wise effort.
 * claude / codex / grok take the value as-is; gemini has no effort control.
 */
export function effortFor(harness: Harness, effort: Effort): string | undefined {
  return harness === "gemini" ? undefined : effort;
}

// ---- roster --------------------------------------------------------------------

/** YAML frontmatter of a markdown file as an object; `{}` when absent or invalid. */
export function parseFrontmatter(path: string): Record<string, unknown> {
  let text: string;
  try {
    text = readFileSync(path, "utf8");
  } catch {
    return {};
  }
  if (!text.startsWith("---\n")) return {};
  const end = text.indexOf("\n---", 4);
  if (end === -1) return {};
  let data: unknown;
  try {
    data = parseYaml(text.slice(4, end));
  } catch {
    return {};
  }
  return isRecord(data) ? data : {};
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The bundled SDLC roster, sorted by file name. */
export function rosterAgents(rosterDir: string = DEFAULT_ROSTER_DIR): RosterAgent[] {
  let files: string[];
  try {
    files = readdirSync(rosterDir).filter((f) => f.endsWith(".md"));
  } catch {
    return [];
  }
  files.sort();
  const items: RosterAgent[] = [];
  for (const file of files) {
    const fm = parseFrontmatter(join(rosterDir, file));
    let tools: unknown = fm["tools"];
    if (typeof tools === "string") {
      tools = tools
        .split(",")
        .map((t) => t.trim())
        .filter((t) => t.length > 0);
    }
    const description = typeof fm["description"] === "string" ? fm["description"].trim() : "";
    items.push({
      name: fm["name"] ? String(fm["name"]) : basename(file, ".md"),
      description: description || null,
      tools: Array.isArray(tools) ? tools.map(String) : [],
      model: fm["model"] ? String(fm["model"]) : "inherit",
      effort: fm["effort"] == null ? null : String(fm["effort"]),
    });
  }
  return items;
}

/** `list-agents` entry point. */
export function cmdListAgents(rosterDir: string = DEFAULT_ROSTER_DIR): RosterAgent[] {
  return rosterAgents(rosterDir);
}

/** Roster role names, for `resolve-team` validation. */
export function rosterNames(rosterDir: string = DEFAULT_ROSTER_DIR): Set<string> {
  return new Set(rosterAgents(rosterDir).map((a) => a.name));
}

// ---- team ----------------------------------------------------------------------

type RawMember = { role: string; lead: boolean; model: string; effort: string };

function str(value: unknown): string {
  return value ? String(value).trim() : "";
}

/** One `agent:` list item -> `{role, lead, model, effort}`. */
export function normalizeMember(item: unknown): RawMember {
  if (typeof item === "string") return { role: item.trim(), lead: false, model: "", effort: "" };
  if (isRecord(item)) {
    return {
      role: str(item["role"]),
      lead: Boolean(item["lead"]),
      model: str(item["model"]),
      effort: str(item["effort"]),
    };
  }
  return { role: "", lead: false, model: "", effort: "" };
}

function typeName(value: unknown): string {
  if (value === null) return "null";
  return Array.isArray(value) ? "list" : typeof value;
}

/**
 * Resolve a step's `agent:` into a normalized, model-resolved team. `step` is
 * the raw step mapping (`agent`, `model`, `effort` are read). See the Python
 * docstring for `mode` semantics; `errors` is non-empty on authoring problems.
 */
export function resolveTeam(step: Record<string, unknown>, opts: TeamOptions = {}): TeamResult {
  const modelOverride = opts.modelOverride ?? "";
  const effortOverride = opts.effortOverride ?? "";
  const teamMode = opts.teamMode ?? "full";
  const raw = step["agent"];
  const stepModel = str(step["model"]);
  const stepEffort = str(step["effort"]);
  const errors: string[] = [];
  let profile = (opts.profile ?? "").trim().toLowerCase();
  if (profile && !(PROFILE_LEVELS as readonly string[]).includes(profile)) {
    errors.push(`--profile: unknown value '${profile}' (low|medium|max)`);
    profile = "";
  }

  let mode: TeamMode;
  let items: unknown[];
  if (raw == null || (typeof raw === "string" && !raw.trim())) {
    mode = "unset";
    items = [];
  } else if (typeof raw === "boolean") {
    // YAML 1.1 coerces off/no -> false and on/yes -> true; only `off` is valid.
    if (raw === false) {
      mode = "off";
      items = [];
    } else {
      mode = "unset";
      items = [];
      errors.push("agent: `on`/`yes`/`true` is not valid (use a role, a list, `auto`, or `off`)");
    }
  } else if (typeof raw === "string") {
    const kw = raw.trim().toLowerCase();
    if (kw === "auto" || kw === "off") {
      mode = kw;
      items = [];
    } else {
      mode = "single";
      items = [raw];
    }
  } else if (Array.isArray(raw)) {
    mode = "team";
    items = raw;
  } else {
    mode = "unset";
    items = [];
    errors.push(`agent: unexpected type ${typeName(raw)}`);
  }

  const members = items.map(normalizeMember);
  if (mode === "team" && members.length === 1) mode = "single";

  const roster = rosterNames(opts.rosterDir);
  let lead: string | null = null;
  const outMembers: TeamMember[] = [];
  for (const m of members) {
    const role = m.role;
    if (!role) {
      errors.push("agent: list item missing a role");
      continue;
    }
    if (role === "auto" || role === "off") {
      errors.push(`'${role}' is a policy keyword; not valid as a team member`);
    } else if (roster.size > 0 && !roster.has(role)) {
      errors.push(`unknown role '${role}' (not in roster)`);
    }
    if (m.lead) {
      if (lead) {
        errors.push(`multiple leads (${lead}, ${role}); only one allowed`);
      } else {
        lead = role;
      }
    }
    const pinModel = modelOverride || m.model || stepModel;
    const pinEffort = effortOverride || m.effort || stepEffort;
    const rm = resolveModelDict(pinModel, pinEffort, profile, opts);
    const member: TeamMember = { ...rm, role, lead: m.lead };
    if (modelOverride || effortOverride) {
      member.reason = rm.reason ? `run tuning override; ${rm.reason}` : "run tuning override";
    }
    outMembers.push(member);
  }

  const result: TeamResult = { mode, lead, members: outMembers, errors };
  if (teamMode !== "full" && teamMode !== "solo") {
    errors.push(`--team-mode: unknown value '${teamMode}' (full|solo)`);
  } else if (teamMode === "solo" && mode === "team" && outMembers.length > 0) {
    // Budget collapse: keep the declared lead (else the first member) and
    // demote to a single dispatch. `collapsed` is additive to the shape.
    const keep = outMembers.find((m) => m.lead) ?? outMembers[0];
    if (keep !== undefined) {
      const dropped = outMembers.filter((m) => m !== keep).map((m) => m.role);
      let note = "team collapsed to lead (solo mode)";
      if (!keep.lead) note += "; no declared lead - first member kept";
      keep.reason = keep.reason ? `${keep.reason}; ${note}` : note;
      result.mode = "single";
      result.lead = keep.lead ? keep.role : null;
      result.members = [keep];
      result.collapsed = { from: outMembers.length, dropped };
    }
  }
  return result;
}

/** Load `defPath`, find the step by id (missing -> `{}`), and resolve its team. */
export function cmdResolveTeam(
  defPath: string,
  stepId: string,
  opts: TeamOptions = {},
): TeamResult {
  const definition: unknown = parseYaml(readFileSync(defPath, "utf8"));
  const steps =
    isRecord(definition) && Array.isArray(definition["steps"]) ? definition["steps"] : [];
  const step = steps.find((s: unknown) => isRecord(s) && s["id"] === stepId);
  return resolveTeam(isRecord(step) ? step : {}, opts);
}
