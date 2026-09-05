// `{{...}}` templating for step definitions. Port of workflows.py `_render_step`
// and `cmd_render`: sequential literal replacement, no expressions, no escaping,
// no whitespace tolerance inside the braces. Unresolved placeholders stay verbatim.

import type { State, Step } from "./types.ts";

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
 *   1. `{{workflow.dir}}` -> `workflowDir`
 *   2. `{{run.dir}}`      -> `runDir` (left verbatim when not supplied)
 *   3. `{{run.id}}`       -> `state.run_id`
 *   4. `{{project.<k>}}`  -> each key of `state.project`
 *   5. `{{<name>}}`       -> `state.inputs` merged under `state.outputs`
 * Outputs go last, so an output key literally named `project.extra` can
 * shadow a still-unresolved project placeholder.
 */
export function render(
  template: string,
  state: State,
  workflowDir: string,
  runDir?: string,
): string {
  let out = template.replaceAll("{{workflow.dir}}", workflowDir);
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
  return out;
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
