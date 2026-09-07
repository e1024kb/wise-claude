// Permission host policy for headless Claude children (D18 revised, v5.0.0-rc.3).
//
// A child runs with `--permission-prompt-tool stdio`: every tool call its permission mode would
// prompt for arrives on stdout as a `control_request` (`can_use_tool`) and the engine answers it
// on stdin. Rules the step pre-granted (`allowed_tools`) never reach here; `full-access` children
// prompt for nothing. What does reach here is decided by shape: reading is allowed, changing
// things is denied unless the step declared it. MCP tool names are classified by their verb, so
// the policy holds for any server the CLI inherits (trackers, chat, docs) without a per-vendor list.

export type PermissionDecision =
  | { behavior: "allow"; updatedInput: unknown }
  | { behavior: "deny"; message: string };

/** Built-in tools that only read; anything else built in is denied unless pre-granted. */
export const READ_ONLY_BUILTINS = new Set([
  "Read",
  "Glob",
  "Grep",
  "LS",
  "WebFetch",
  "WebSearch",
  "ToolSearch",
  "TodoWrite",
  "TodoRead",
  "Skill",
  "ListMcpResourcesTool",
  "ReadMcpResourceTool",
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
  "remove",
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
  "step's `allowed_tools` or run with `permissions: full`";

/** The engine's answer to one `can_use_tool` request. */
export function decidePermission(toolName: string, input: unknown): PermissionDecision {
  if (READ_ONLY_BUILTINS.has(toolName) || isReadMcpTool(toolName)) {
    return { behavior: "allow", updatedInput: input };
  }
  return { behavior: "deny", message: `${toolName} ${DENY_HINT}` };
}
