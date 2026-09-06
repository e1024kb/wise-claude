// `{{...}}` templating for step definitions. Port of workflows.py `_render_step`
// and `cmd_render`: sequential literal replacement, no expressions, no escaping,
// no whitespace tolerance inside the braces. Unresolved placeholders stay verbatim.

import { usageTotal } from "./ledger.ts";
import type { State, Step } from "./types.ts";
import { PLUGIN_ROOT } from "./version.ts";

/** Python `str(v)` stand-in for recorded output values. */
function stringify(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/**
 * Render one template string against the run state.
 *
 * Substitution order mirrors v1 (each pass is a plain global replace):
 *   0. `${CLAUDE_PLUGIN_ROOT}` -> the plugin root (children have no such variable; the
 *      bundled prompts point at `references/` and `agents/` files with it)
 *   1. `{{workflow.dir}}` -> `workflowDir`
 *   2. `{{run.dir}}`      -> `runDir` (left verbatim when not supplied)
 *   3. `{{run.id}}`       -> `state.run_id`
 *   4. `{{project.<k>}}`  -> each key of `state.project`
 *   5. `{{<name>}}`       -> `state.inputs` merged under `state.outputs`
 *   6. `{{usage}}`        -> the run's usage views as JSON (M6.1), unless an output took it
 * Outputs go last, so an output key literally named `project.extra` can
 * shadow a still-unresolved project placeholder.
 */
export function render(
  template: string,
  state: State,
  workflowDir: string,
  runDir?: string,
): string {
  let out = template.replaceAll("${CLAUDE_PLUGIN_ROOT}", PLUGIN_ROOT);
  out = out.replaceAll("{{workflow.dir}}", workflowDir);
  if (runDir !== undefined) out = out.replaceAll("{{run.dir}}", runDir);
  out = out.replaceAll("{{run.id}}", state.run_id);
  for (const [k, v] of Object.entries(state.project ?? {})) {
    out = out.replaceAll(`{{project.${k}}}`, stringify(v));
  }
  // v1 merged pre-flight inputs into outputs; a later recorded output wins.
  const named: Record<string, unknown> = { ...state.inputs, ...state.outputs };
  for (const [k, v] of Object.entries(named)) {
    out = out.replaceAll(`{{${k}}}`, stringify(v));
  }
  if (out.includes("{{usage}}")) out = out.replaceAll("{{usage}}", usageJson(state));
  return out;
}

/** The totals a `report` step receives as data (E5): total, per pool, per harness, per step. */
export function usageJson(state: State): string {
  const u = state.usage;
  return JSON.stringify(
    {
      total: usageTotal(u),
      by_pool: { subscription: u.subscription, "api-key": u["api-key"] },
      by_harness: u.by_harness,
      by_step: u.by_step ?? {},
    },
    null,
    2,
  );
}

/** Recursively render every string inside a step value (lists and maps included). */
function renderValue(value: unknown, state: State, workflowDir: string, runDir?: string): unknown {
  if (typeof value === "string") return render(value, state, workflowDir, runDir);
  if (Array.isArray(value)) return value.map((x) => renderValue(x, state, workflowDir, runDir));
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) out[k] = renderValue(v, state, workflowDir, runDir);
    return out;
  }
  return value;
}

/**
 * Return a copy of `step` with every string field rendered, recursing into
 * lists and maps, the same way v1 rendered the whole step definition
 * (`prompt`, `run`, `message`, `options`, `groups`, ... all included).
 */
export function renderStep(step: Step, state: State, workflowDir: string, runDir?: string): Step {
  return renderValue(step, state, workflowDir, runDir) as Step;
}

/**
 * Render a template against a flat variable dict, the same sequential literal replacement as
 * `render` (no expressions, no escaping). Used by the unit phases (M4.2), whose variables are
 * per unit and per phase rather than run state. Keys are matched verbatim (`unit.ref`,
 * `plan_path`, ...); unresolved placeholders stay in place for the caller to detect.
 */
export function renderVars(template: string, vars: Record<string, unknown>): string {
  let out = template;
  for (const [k, v] of Object.entries(vars)) out = out.replaceAll(`{{${k}}}`, stringify(v));
  return out;
}

/** Placeholders still unresolved after rendering, in order of appearance, deduplicated. */
export function unresolvedPlaceholders(text: string): string[] {
  const seen = new Set<string>();
  for (const m of text.matchAll(/\{\{([^{}]+)\}\}/g)) seen.add(m[1] ?? "");
  return [...seen];
}
