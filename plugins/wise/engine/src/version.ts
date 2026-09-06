import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, relative } from "node:path";

/** Plugin root: `plugins/wise`. The engine lives in `plugins/wise/engine`. */
export const PLUGIN_ROOT: string = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
/** `plugins/wise/engine`: holds `engine.sh`, the entry the child MCP config runs. */
export const ENGINE_ROOT: string = join(PLUGIN_ROOT, "engine");

/** Single version source is `plugins/wise/.claude-plugin/plugin.json`. */
export function pluginVersion(): string {
  const raw = readFileSync(join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), "utf8");
  const parsed = JSON.parse(raw) as { version?: unknown };
  if (typeof parsed.version !== "string") {
    throw new Error("plugin.json has no string `version`");
  }
  return parsed.version;
}

let cachedBuildId: string | undefined;

/**
 * `<plugin version>+<sha1 of engine/src/**\/*.ts, 10 hex>`: what the daemon handshake compares.
 * The plugin version alone misses every code change inside one version (a daemon started
 * before an edit kept serving the old code), and file mtimes would differ between the repo
 * checkout and the installed copy of the same code; content does not.
 */
export function buildId(): string {
  if (cachedBuildId !== undefined) return cachedBuildId;
  cachedBuildId = sourceBuildId();
  return cachedBuildId;
}

/**
 * The build id of the sources on disk right now, never cached: a long-lived process (the MCP
 * server of a desktop session) uses it to notice that the plugin copy under it was updated and
 * the daemon it talks to is stale.
 */
export function sourceBuildId(): string {
  const root = join(ENGINE_ROOT, "src");
  const files: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.name.endsWith(".ts")) files.push(path);
    }
  };
  walk(root);
  const hash = createHash("sha1");
  for (const path of files.toSorted()) {
    hash.update(relative(root, path));
    hash.update("\0");
    hash.update(readFileSync(path));
    hash.update("\0");
  }
  return `${pluginVersion()}+${hash.digest("hex").slice(0, 10)}`;
}

export function runtimeName(): "bun" | "node" {
  return typeof (globalThis as { Bun?: unknown }).Bun === "undefined" ? "node" : "bun";
}
