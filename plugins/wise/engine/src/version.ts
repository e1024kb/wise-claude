import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

/** Plugin root: `plugins/wise`. The engine lives in `plugins/wise/engine`. */
export const PLUGIN_ROOT: string = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

/** Single version source is `plugins/wise/.claude-plugin/plugin.json`. */
export function pluginVersion(): string {
  const raw = readFileSync(join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), "utf8");
  const parsed = JSON.parse(raw) as { version?: unknown };
  if (typeof parsed.version !== "string") {
    throw new Error("plugin.json has no string `version`");
  }
  return parsed.version;
}

export function runtimeName(): "bun" | "node" {
  return typeof (globalThis as { Bun?: unknown }).Bun === "undefined" ? "node" : "bun";
}
