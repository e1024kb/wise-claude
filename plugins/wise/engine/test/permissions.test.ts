import { test } from "node:test";
import assert from "node:assert/strict";
import { decidePermission, isReadMcpTool, mcpToolPart, nameTokens } from "../src/permissions.ts";

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
    /^Bash .*allowed_tools.*permissions: full/,
  );
  assert.equal(decidePermission("Edit", {}).behavior, "deny");
  assert.equal(decidePermission("mcp__slack__slack_send_message", {}).behavior, "deny");
  // A built-in read-only tool: denying it would break resource directory reads in headless
  // children.
  assert.equal(decidePermission("ReadMcpResourceDirTool", {}).behavior, "allow");
});

test("decidePermission: Skill and TodoWrite are not auto-allowed", () => {
  // `Skill` can activate a skill whose own `allowed-tools` pre-approves mutating tools without
  // ever reaching this decision; `TodoWrite` writes state. Neither belongs in READ_ONLY_BUILTINS.
  assert.equal(decidePermission("Skill", {}).behavior, "deny");
  assert.equal(decidePermission("TodoWrite", {}).behavior, "deny");
  assert.equal(decidePermission("TodoRead", {}).behavior, "allow");
});
