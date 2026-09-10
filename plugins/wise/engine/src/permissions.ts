// Permission host policy for headless Claude children (D18 revised, v5.0.0-rc.3).
//
// A child runs with `--permission-prompt-tool stdio`: every tool call its permission mode would
// prompt for arrives on stdout as a `control_request` (`can_use_tool`) and the engine answers it
// on stdin. Rules the step pre-granted (`allowed_tools`) never reach here; `full-access` children
// prompt for nothing. What does reach here is decided by shape: reading is allowed. Under
// `approval-required`, changing things is denied unless the step declared it. Under `auto`,
// ordinary local mutations and shell commands are allowed while destructive commands and
// mutating external MCP calls stay denied.
// `full-access` is normally handled by the CLI itself, but is accepted here for completeness.

import type { Harness, Permissions, RunMode, State } from "./types.ts";

export type PermissionDecision =
  | { behavior: "allow"; updatedInput: unknown }
  | { behavior: "deny"; message: string };

const MODE_RANK: Readonly<Record<RunMode, number>> = {
  "approval-required": 0,
  auto: 1,
  "full-access": 2,
};

/** Apply a provider-wide permission floor without weakening a step that asks for more. */
export function effectiveMode(step: RunMode | undefined, floor: RunMode): RunMode {
  // An omitted step mode inherits the provider choice. An explicit step mode is a requirement,
  // so it may raise that choice but never weaken it.
  const wanted = step ?? floor;
  return MODE_RANK[wanted] >= MODE_RANK[floor] ? wanted : floor;
}

/** New per-provider state first; legacy run-wide state remains resumable. */
export function providerPermission(
  state: Pick<State, "provider_permissions" | "permissions">,
  harness: Harness,
): RunMode {
  const selected = state.provider_permissions?.[harness];
  if (selected !== undefined) return selected;
  const legacy: Permissions | undefined = state.permissions;
  if (legacy === "full") return "full-access";
  if (legacy === "allowlist") return "approval-required";
  return "auto";
}

/**
 * Built-in tools that only read; anything else built in is denied unless pre-granted.
 *
 * `Skill` and `TodoWrite` are deliberately absent: a skill's own `allowed-tools` frontmatter
 * pre-approves tools for the turn that invokes it, so auto-allowing `Skill` would let a step reach
 * a mutating tool without ever hitting `decidePermission`; `TodoWrite` writes state, so it fails
 * the "only read" bar this set exists to hold.
 */
export const READ_ONLY_BUILTINS = new Set([
  "Read",
  "Glob",
  "Grep",
  "LS",
  "WebFetch",
  "WebSearch",
  "ToolSearch",
  "TodoRead",
  "ListMcpResourcesTool",
  "ReadMcpResourceTool",
  "ReadMcpResourceDirTool",
]);

/** First-position verbs that mark an MCP tool as mutating, whatever follows. */
export const MUTATING_VERBS = new Set([
  "create",
  "update",
  "delete",
  "remove",
  "send",
  "post",
  "write",
  "edit",
  "set",
  "add",
  "move",
  "archive",
  "reply",
  "forward",
  "trash",
  "untrash",
  "label",
  "unlabel",
  "mark",
  "unmark",
  "merge",
  "push",
  "comment",
  "assign",
  "transition",
  "resolve",
  "close",
  "reopen",
  "schedule",
  "invite",
  "upload",
  "complete",
  "escalate",
  "acknowledge",
  "apply",
  "change",
  "toggle",
  "link",
  "unlink",
  "import",
  "respond",
  "execute",
  "run",
  "autofill",
  "enter",
  "release",
  "request",
  "cancel",
  "nudge",
  "answer",
  "resume",
  "start",
  "stop",
  "kill",
  "submit",
  "publish",
  "deploy",
  "install",
  "rename",
  "clear",
  "reset",
  "sign",
  "fill",
  "pay",
  "transfer",
  "trigger",
  "put",
  "patch",
]);

/** Verbs that mark an MCP tool as read-only when they lead the name (or follow a vendor prefix). */
export const READ_VERBS = new Set([
  "get",
  "list",
  "read",
  "search",
  "fetch",
  "view",
  "find",
  "query",
  "describe",
  "show",
  "lookup",
  "browse",
  "preview",
  "check",
  "count",
  "explain",
  "summarize",
  "summarise",
  "inspect",
  "peek",
  "load",
  "retrieve",
  "poll",
  "watch",
  "status",
  "health",
  "is",
  "has",
  "who",
  "what",
]);

/** `getJiraIssue` → `["get","jira","issue"]`; `slack_read-channel` → `["slack","read","channel"]`. */
export function nameTokens(toolPart: string): string[] {
  return toolPart
    .replaceAll(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replaceAll(/[-.\s]+/g, "_")
    .toLowerCase()
    .split("_")
    .filter((t) => t.length > 0);
}

/** `mcp__<server>__<tool>` → the tool part; a bare `mcp__<server>` has none. */
export function mcpToolPart(toolName: string): string | undefined {
  if (!toolName.startsWith("mcp__")) return undefined;
  const rest = toolName.slice(5);
  const i = rest.indexOf("__");
  return i < 0 ? "" : rest.slice(i + 2);
}

/** Read-shaped MCP tool: a read verb first, or second behind a vendor prefix that is no verb. */
export function isReadMcpTool(toolName: string): boolean {
  const part = mcpToolPart(toolName);
  if (part === undefined || part.length === 0) return false;
  const [first, second] = nameTokens(part);
  if (first === undefined) return false;
  if (MUTATING_VERBS.has(first)) return false;
  if (READ_VERBS.has(first)) return true;
  if (second === undefined) return false;
  if (MUTATING_VERBS.has(second)) return false;
  return READ_VERBS.has(second);
}

const DENY_HINT =
  "not granted to this step by wise; a read-shaped tool would be allowed, add the rule to the " +
  "step's `allowed_tools` or select Bypass permissions for this provider";

export const AUTO_MUTATING_BUILTINS = new Set([
  "Edit",
  "Write",
  "MultiEdit",
  "NotebookEdit",
  "TodoWrite",
]);

/** Commands that `auto` never approves; bypass remains an explicit user choice. */
export const DESTRUCTIVE_COMMAND_RE =
  /(^|[\n;&|]\s*)(sudo\b|rm\s+(?:-[^\s]*r[^\s]*|--recursive)\b|git\s+reset\s+--hard\b|git\s+clean\s+[^\n]*-[^\n\s]*f|chmod\s+-R\b|chown\s+-R\b|mkfs\b|dd\s+if=|shutdown\b|reboot\b)/i;

function commandOf(input: unknown): string {
  if (typeof input !== "object" || input === null || Array.isArray(input)) return "";
  const command = (input as Record<string, unknown>).command;
  return typeof command === "string" ? command : "";
}

export type PermissionOpts = { mode?: RunMode };

/** The engine's answer to one `can_use_tool` request. */
export function decidePermission(
  toolName: string,
  input: unknown,
  opts: PermissionOpts = {},
): PermissionDecision {
  const mode = opts.mode ?? "approval-required";
  if (mode === "full-access") return { behavior: "allow", updatedInput: input };
  if (READ_ONLY_BUILTINS.has(toolName) || isReadMcpTool(toolName)) {
    return { behavior: "allow", updatedInput: input };
  }
  if (mode === "auto") {
    if (AUTO_MUTATING_BUILTINS.has(toolName)) return { behavior: "allow", updatedInput: input };
    if (toolName === "Bash") {
      const command = commandOf(input);
      if (command && !DESTRUCTIVE_COMMAND_RE.test(command)) {
        return { behavior: "allow", updatedInput: input };
      }
      return {
        behavior: "deny",
        message: `Bash command blocked by wise auto mode; select Bypass permissions to run it`,
      };
    }
  }
  return { behavior: "deny", message: `${toolName} ${DENY_HINT}` };
}
