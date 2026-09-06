// `pr`: create the PR or refresh the existing one's body (ensure-pr-auto.md). The body comes
// from the repo's PR template when one exists (known sections filled mechanically, the rest kept
// verbatim) else a compact default: summary, changes, ticket link, testing. No model involved,
// so the text is data the engine has: title, commit subjects, the ticket link.

import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { errText, fail, gh, git, isProtectedBranch, jsonOf, ok, pass } from "./common.ts";
import type { PhaseCtx, PhaseResult, PhaseRunner } from "./common.ts";

const TITLE_MAX = 90;
const COMMITS_MAX = 20;

// ---- template --------------------------------------------------------------------------------

/** draft-body.md §4 ladder; `undefined` when the repo ships no template. */
export function findPrTemplate(repo: string): string | undefined {
  for (const rel of [
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "docs/pull_request_template.md",
  ]) {
    const p = join(repo, rel);
    if (existsSync(p)) return p;
  }
  const dir = join(repo, ".github", "PULL_REQUEST_TEMPLATE");
  if (existsSync(dir)) {
    const dflt = join(dir, "default.md");
    if (existsSync(dflt)) return dflt;
    const first = readdirSync(dir)
      .filter((n) => n.endsWith(".md"))
      .toSorted()[0];
    if (first !== undefined) return join(dir, first);
  }
  return undefined;
}

export type PrFacts = {
  ref: string;
  title: string;
  /** Markdown link or plain code span for the ticket. */
  ticketLink: string;
  commits: string[];
  planPath?: string;
};

function summaryText(f: PrFacts): string {
  return `- ${f.title}`;
}
function changesText(f: PrFacts): string {
  if (f.commits.length === 0) return "- no commits on the branch yet";
  return f.commits.map((c) => `- ${c}`).join("\n");
}
function contextText(f: PrFacts): string {
  const lines = [`- ticket: ${f.ticketLink}`];
  if (f.planPath !== undefined) lines.push(`- plan: \`${f.planPath}\``);
  return lines.join("\n");
}

/** The compact default body (no template in the repo). */
export function defaultPrBody(f: PrFacts): string {
  return [
    "## Summary",
    summaryText(f),
    "",
    "## Changes",
    changesText(f),
    "",
    "## Context",
    contextText(f),
    "",
    "## Testing",
    "- [ ] Unit tests pass",
    "- [ ] Manual verification",
    "",
  ].join("\n");
}

/** Fill the sections the engine can fill; keep every other section of the template verbatim. */
export function fillPrTemplate(template: string, f: PrFacts): string {
  const lines = template.split("\n");
  const out: string[] = [];
  let skipping = false;
  let matched = false;
  for (const line of lines) {
    const m = /^##\s+(.+?)\s*$/.exec(line);
    if (m) {
      skipping = false;
      out.push(line);
      const head = (m[1] ?? "").toLowerCase();
      let filled: string | undefined;
      if (head.startsWith("summary")) filled = summaryText(f);
      else if (head.startsWith("change")) filled = changesText(f);
      else if (/^(context|ticket|reference)/.test(head)) filled = contextText(f);
      if (filled !== undefined) {
        out.push(filled, "");
        skipping = true;
        matched = true;
      }
      continue;
    }
    if (!skipping) out.push(line);
  }
  if (!matched) return `${defaultPrBody(f)}\n${template}`;
  return out.join("\n").replace(/\n{3,}/g, "\n\n");
}

// ---- facts -------------------------------------------------------------------------------------

function clipTitle(s: string): string {
  const flat = s
    .replaceAll(/\s+/g, " ")
    .trim()
    .replace(/[.:;,]+$/, "");
  return flat.length > TITLE_MAX ? flat.slice(0, TITLE_MAX - 1) + "…" : flat;
}

function planHeading(planPath: string): string | undefined {
  try {
    const first = readFileSync(planPath, "utf8")
      .split("\n")
      .find((l) => /^#\s+\S/.test(l));
    return first?.replace(/^#\s+/, "").trim();
  } catch {
    return undefined;
  }
}

async function collectFacts(ctx: PhaseCtx): Promise<PrFacts> {
  const { unit } = ctx;
  const log = await git(ctx, [
    "log",
    "--pretty=%s",
    `--max-count=${COMMITS_MAX}`,
    `origin/${unit.base || "main"}..${unit.branch}`,
  ]);
  const commits = ok(log)
    ? log.stdout
        .split("\n")
        .map((l) => l.trim())
        .filter((l) => l.length > 0)
    : [];
  const ticket = ctx.config.tickets.find((t) => t.ref === unit.ref);
  let title: string;
  if (ctx.config.pipeline === "plan") {
    const heading = unit.plan_path !== undefined ? planHeading(unit.plan_path) : undefined;
    title = heading !== undefined ? `${unit.ref}: ${heading}` : unit.ref;
  } else if (ticket?.title) {
    title = `${unit.ref}: ${ticket.title}`;
  } else if (commits[0] !== undefined) {
    title = `${unit.ref}: ${commits[0]}`;
  } else {
    title = unit.ref;
  }
  const ticketLink = ticket?.url ? `[${unit.ref}](${ticket.url})` : `\`${unit.ref}\``;
  const facts: PrFacts = { ref: unit.ref, title: clipTitle(title), ticketLink, commits };
  if (unit.plan_path !== undefined) facts.planPath = unit.plan_path;
  return facts;
}

function writeBody(ctx: PhaseCtx, body: string): string {
  const dir = join(ctx.runDir, "units");
  mkdirSync(dir, { recursive: true });
  const path = join(dir, `${encodeURIComponent(ctx.unit.branch)}.pr-body.md`);
  writeFileSync(path, body, "utf8");
  return path;
}

// ---- phase ------------------------------------------------------------------------------------------

type PrView = { number?: unknown; url?: unknown; state?: unknown; baseRefName?: unknown };

/** `gh pr view <branch>`: the PR on this head, any state; `undefined` when there is none. */
export async function viewPr(
  ctx: PhaseCtx,
  branch: string,
): Promise<{ number: number; url: string; state: string; base?: string } | undefined> {
  const r = await gh(ctx, ["pr", "view", branch, "--json", "number,url,state,baseRefName"]);
  const v = jsonOf(r) as PrView | undefined;
  if (!v || typeof v.number !== "number" || typeof v.url !== "string") return undefined;
  const out: { number: number; url: string; state: string; base?: string } = {
    number: v.number,
    url: v.url,
    state: typeof v.state === "string" ? v.state : "OPEN",
  };
  if (typeof v.baseRefName === "string") out.base = v.baseRefName;
  return out;
}

export const prPhase: PhaseRunner = async (ctx): Promise<PhaseResult> => {
  const { unit } = ctx;
  if (isProtectedBranch(unit.branch))
    return fail(`pr: refused, ${unit.branch} is a protected branch`);

  const existing = await viewPr(ctx, unit.branch);
  if (existing?.state === "MERGED") {
    const pr = { number: existing.number, url: existing.url };
    return fail(`pr-merged: #${pr.number}`, "merged", { unit: { ...unit, pr } });
  }
  if (existing?.state === "CLOSED") {
    const pr = { number: existing.number, url: existing.url };
    return fail("pr-closed: closed without merge", "human-intervention", { unit: { ...unit, pr } });
  }

  const facts = await collectFacts(ctx);
  const templatePath = findPrTemplate(ctx.cwd);
  const body =
    templatePath !== undefined
      ? fillPrTemplate(readFileSync(templatePath, "utf8"), facts)
      : defaultPrBody(facts);
  ctx.log(`pr: body from ${templatePath ?? "the compact default"}`);
  const bodyPath = writeBody(ctx, body);

  if (existing) {
    const edit = await gh(ctx, ["pr", "edit", String(existing.number), "--body-file", bodyPath]);
    if (!ok(edit)) return fail(`pr: refresh of #${existing.number} failed: ${errText(edit)}`);
    ctx.log(`pr: refreshed #${existing.number}`);
    return pass({ unit: { ...unit, pr: { number: existing.number, url: existing.url } } });
  }

  const created = await gh(ctx, [
    "pr",
    "create",
    "--base",
    unit.base || "main",
    "--head",
    unit.branch,
    "--title",
    facts.title,
    "--body-file",
    bodyPath,
  ]);
  if (!ok(created)) return fail(`pr: create failed: ${errText(created)}`);
  const url = created.stdout
    .split("\n")
    .map((l) => l.trim())
    .find((l) => /^https?:\/\//.test(l));
  const numFromUrl = url ? Number(url.split("/").at(-1)) : Number.NaN;
  let pr: { number: number; url: string } | undefined =
    url !== undefined && Number.isInteger(numFromUrl) ? { number: numFromUrl, url } : undefined;
  if (pr === undefined) {
    const view = await viewPr(ctx, unit.branch);
    if (view) pr = { number: view.number, url: view.url };
  }
  if (pr === undefined) return fail("pr: created but could not read its number and url");
  ctx.log(`pr: created #${pr.number} ${pr.url}`);
  return pass({ unit: { ...unit, pr } });
};
