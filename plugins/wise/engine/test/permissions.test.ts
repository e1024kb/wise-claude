import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  decidePermission,
  effectiveMode,
  isAutoBashCommand,
  isReadMcpTool,
  mcpToolPart,
  nameTokens,
  providerPermission,
} from "../src/permissions.ts";

test("nameTokens: camelCase, snake_case and kebab-case split the same way", () => {
  assert.deepEqual(nameTokens("getJiraIssue"), ["get", "jira", "issue"]);
  assert.deepEqual(nameTokens("slack_read-channel"), ["slack", "read", "channel"]);
  assert.deepEqual(nameTokens("searchJiraIssuesUsingJql"), [
    "search",
    "jira",
    "issues",
    "using",
    "jql",
  ]);
  assert.deepEqual(nameTokens("query-docs"), ["query", "docs"]);
});

test("mcpToolPart: server and tool split", () => {
  assert.equal(mcpToolPart("mcp__jira__getJiraIssue"), "getJiraIssue");
  assert.equal(mcpToolPart("mcp__plugin_linear_linear__get_issue"), "get_issue");
  assert.equal(mcpToolPart("mcp__wise-engine"), "");
  assert.equal(mcpToolPart("WebFetch"), undefined);
});

test("isReadMcpTool: read verbs allow, mutating verbs deny, vendor prefixes are looked through", () => {
  const read = [
    "mcp__jira__getJiraIssue",
    "mcp__jira__searchJiraIssuesUsingJql",
    "mcp__plugin_linear_linear__list_issues",
    "mcp__691668ad__slack_read_channel",
    "mcp__691668ad__slack_search_public",
    "mcp__plugin_context7_context7__query-docs",
    "mcp__9614c717__list_events",
    "mcp__betterstack__status_page",
  ];
  for (const t of read) assert.equal(isReadMcpTool(t), true, t);
  const mutating = [
    "mcp__691668ad__slack_send_message",
    "mcp__jira__createJiraIssue",
    "mcp__jira__transitionJiraIssue",
    "mcp__plugin_linear_linear__update_issue",
    "mcp__betterstack__update_status_page",
    "mcp__betterstack__resolve_incident",
    "mcp__f63586e2__trash_message",
    "mcp__x__slack_send_message_draft",
    "mcp__x__unknownVerb",
    "mcp__wise-engine",
  ];
  for (const t of mutating) assert.equal(isReadMcpTool(t), false, t);
});

test("decidePermission: read-only built-ins and read MCP tools allow with the input unchanged; the rest deny with a hint", () => {
  const input = { url: "https://example.com" };
  assert.deepEqual(decidePermission("WebFetch", input), { behavior: "allow", updatedInput: input });
  assert.deepEqual(decidePermission("mcp__jira__getJiraIssue", { key: "X-1" }), {
    behavior: "allow",
    updatedInput: { key: "X-1" },
  });
  const bash = decidePermission("Bash", { command: "rm -rf x" });
  assert.equal(bash.behavior, "deny");
  assert.match(
    bash.behavior === "deny" ? bash.message : "",
    /^Bash .*allowed_tools.*Bypass permissions/,
  );
  assert.equal(decidePermission("Edit", {}).behavior, "deny");
  assert.equal(decidePermission("mcp__slack__slack_send_message", {}).behavior, "deny");
  // A built-in read-only tool: denying it would break resource directory reads in headless
  // children.
  assert.equal(decidePermission("ReadMcpResourceDirTool", {}).behavior, "allow");
});

test("decidePermission: approval-required stays read-only; auto allows local edits and safe commands", (t) => {
  // `Skill` can activate a skill whose own `allowed-tools` pre-approves mutating tools without
  // ever reaching this decision; `TodoWrite` writes state. Neither belongs in READ_ONLY_BUILTINS.
  assert.equal(decidePermission("Skill", {}).behavior, "deny");
  assert.equal(decidePermission("TodoWrite", {}).behavior, "deny");
  assert.equal(decidePermission("TodoRead", {}).behavior, "allow");
  const scratch = mkdtempSync(join(tmpdir(), "wise-permissions-auto-"));
  t.after(() => rmSync(scratch, { recursive: true, force: true }));
  const workspace = join(scratch, "project");
  const shared = join(scratch, "shared");
  mkdirSync(workspace);
  mkdirSync(shared);
  const auto = { mode: "auto" as const, workspaceRoots: [workspace, shared] };
  assert.equal(decidePermission("Edit", { file_path: "src/a.ts" }, auto).behavior, "allow");
  assert.equal(
    decidePermission("Write", { file_path: join(workspace, "src/a.ts") }, auto).behavior,
    "allow",
  );
  assert.equal(
    decidePermission("MultiEdit", { file_path: join(shared, "a.ts") }, auto).behavior,
    "allow",
  );
  assert.equal(
    decidePermission("NotebookEdit", { notebook_path: "notebooks/a.ipynb" }, auto).behavior,
    "allow",
  );
  assert.equal(decidePermission("Edit", { file_path: "../outside.ts" }, auto).behavior, "deny");
  assert.equal(
    decidePermission("Write", { file_path: join(scratch, "project-other/a.ts") }, auto).behavior,
    "deny",
  );
  assert.equal(decidePermission("MultiEdit", {}, auto).behavior, "deny");
  assert.equal(decidePermission("TodoWrite", {}, { mode: "auto" }).behavior, "allow");
  assert.equal(
    decidePermission("Bash", { command: "npm test" }, { mode: "auto" }).behavior,
    "deny",
  );
  assert.equal(
    decidePermission("Bash", { command: "rm -rf ./dist" }, { mode: "auto" }).behavior,
    "deny",
  );
  assert.equal(
    decidePermission("Bash", { command: "echo ready\nrm -rf ./dist" }, { mode: "auto" }).behavior,
    "deny",
  );
  assert.equal(
    decidePermission("Bash", { command: "sh -c 'rm -rf ./dist'" }, { mode: "auto" }).behavior,
    "deny",
  );
  assert.equal(
    decidePermission("Bash", { command: 'bash -c "rm -rf ./dist"' }, { mode: "auto" }).behavior,
    "deny",
  );
  assert.equal(
    decidePermission("mcp__slack__slack_send_message", {}, { mode: "auto" }).behavior,
    "deny",
  );
  assert.equal(decidePermission("Skill", {}, { mode: "auto" }).behavior, "deny");
  assert.equal(isAutoBashCommand("git status --short"), true);
  assert.equal(isAutoBashCommand("git branch --show-current"), true);
  assert.equal(isAutoBashCommand("rg needle src"), true);
  assert.equal(isAutoBashCommand("git branch feature"), false);
  assert.equal(isAutoBashCommand("git diff --output=review.diff"), false);
  assert.equal(isAutoBashCommand("git diff --output review.diff"), false);
  assert.equal(isAutoBashCommand("git diff --output=/tmp/wise-output"), false);
  assert.equal(isAutoBashCommand("git diff --output /tmp/wise-output"), false);
  assert.equal(isAutoBashCommand("git diff --output-indicator-new=+"), true);
  assert.equal(isAutoBashCommand("rg --pre sh needle ."), false);
  assert.equal(isAutoBashCommand("cat /etc/passwd"), false);
  assert.equal(isAutoBashCommand('cat "/etc/passwd"'), false);
  assert.equal(isAutoBashCommand("cat foo/../../etc/passwd"), false);
  assert.equal(isAutoBashCommand("cat foo/../bar"), false);
  for (const command of [
    "npm test",
    "npm run build",
    "pnpm run build",
    "yarn lint",
    "bun test",
    "just check",
    "make test",
    "cargo test",
    "go test ./...",
    "pytest",
    "ruff check .",
    "eslint .",
    "tsc --noEmit",
  ]) {
    assert.equal(isAutoBashCommand(command), false, command);
  }
  assert.equal(
    decidePermission("Bash", { command: "npm test" }, { mode: "full-access" }).behavior,
    "allow",
  );
});

test("decidePermission: auto-mode file mutations cannot escape through symlinks", (t) => {
  const scratch = mkdtempSync(join(tmpdir(), "wise-permissions-"));
  t.after(() => rmSync(scratch, { recursive: true, force: true }));
  const workspace = join(scratch, "workspace");
  const outside = join(scratch, "outside");
  const safe = join(workspace, "safe");
  mkdirSync(safe, { recursive: true });
  mkdirSync(outside);
  symlinkSync(outside, join(workspace, "escape"), "dir");
  symlinkSync(safe, join(workspace, "safe-link"), "dir");
  symlinkSync(join(outside, "missing.ts"), join(workspace, "dangling.ts"));
  const auto = { mode: "auto" as const, workspaceRoots: [workspace] };

  assert.equal(
    decidePermission("Write", { file_path: join(workspace, "escape", "new.ts") }, auto).behavior,
    "deny",
  );
  assert.equal(
    decidePermission("Edit", { file_path: join(workspace, "dangling.ts") }, auto).behavior,
    "deny",
  );
  assert.equal(
    decidePermission("Write", { file_path: join(workspace, "safe-link", "new.ts") }, auto).behavior,
    "allow",
  );
});

test("permission floors preserve stronger step requirements and support legacy state", () => {
  assert.equal(effectiveMode("approval-required", "auto"), "auto");
  assert.equal(effectiveMode("full-access", "auto"), "full-access");
  assert.equal(effectiveMode(undefined, "approval-required"), "approval-required");
  assert.equal(
    providerPermission({ provider_permissions: { cursor: "full-access" } }, "cursor"),
    "full-access",
  );
  assert.equal(providerPermission({ permissions: "full" }, "claude"), "full-access");
  assert.equal(providerPermission({ permissions: "allowlist" }, "codex"), "approval-required");
  assert.equal(providerPermission({}, "gemini"), "auto");
});
