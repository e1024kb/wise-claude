// Run context on disk (v5.0.0-rc.3): the conductor fetches each ticket once and passes its body
// through `wise_run`; the engine writes it to `<run dir>/context/tickets/<ref>.md` at run creation
// and keeps only `{ref, title, url, path}` in `state.context`. Children read the file on demand
// (whole or by section) instead of receiving the body in every prompt and `wise_context` call,
// and later steps may drop their own markdown into `context/` for the steps after them.

import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { Context, ContextTicket } from "./types.ts";

export const CONTEXT_DIR = "context";
export const TICKETS_DIR = "tickets";
export const INDEX_FILE = "index.md";

export function contextDir(runDir: string): string {
  return join(runDir, CONTEXT_DIR);
}

/** `<run dir>/context/tickets/<ref>.md`; the ref is URI-encoded like the unit ledger files. */
export function ticketFilePath(runDir: string, ref: string): string {
  return join(contextDir(runDir), TICKETS_DIR, `${encodeURIComponent(ref)}.md`);
}

function writeAtomic(path: string, text: string): void {
  const tmp = `${path}.tmp-${process.pid}`;
  writeFileSync(tmp, text, "utf8");
  renameSync(tmp, path);
}

function yamlString(v: string): string {
  return JSON.stringify(v);
}

/** The markdown for one ticket: front matter, an H1, the body as the conductor composed it. */
export function ticketMarkdown(t: ContextTicket, fetchedAt: string): string {
  const fm = [`ref: ${yamlString(t.ref)}`];
  if (t.title) fm.push(`title: ${yamlString(t.title)}`);
  if (t.url) fm.push(`url: ${yamlString(t.url)}`);
  fm.push(`fetched_at: ${yamlString(fetchedAt)}`, "source: conductor");
  const heading = t.title ? `# ${t.ref}: ${t.title}` : `# ${t.ref}`;
  return `---\n${fm.join("\n")}\n---\n\n${heading}\n\n${(t.body ?? "").trim()}\n`;
}

function indexMarkdown(tickets: ContextTicket[]): string {
  const rows = tickets.map((t) => {
    const title = t.title ? ` ${t.title}` : "";
    const url = t.url ? ` (${t.url})` : "";
    return `- ${t.ref}:${title}${url} -> ${TICKETS_DIR}/${encodeURIComponent(t.ref)}.md`;
  });
  return [
    "# Run context",
    "",
    "Files the conductor fetched before the run started. Read the file you need; the body is not",
    "repeated in prompts or in `wise_context`.",
    "",
    "## Tickets",
    "",
    ...rows,
    "",
  ].join("\n");
}

export type PersistOpts = { now?: () => string };

/**
 * Coalesce duplicate `ref`s, last one wins. `ticketFilePath` derives its filename from `ref`
 * alone, so two tickets sharing a ref would write the same file in turn; keeping only the last
 * one keeps the returned entry's `path` matching the body actually left on disk.
 */
function dedupeByRef(tickets: ContextTicket[]): ContextTicket[] {
  const byRef = new Map<string, ContextTicket>();
  for (const t of tickets) byRef.set(t.ref, t);
  return [...byRef.values()];
}

/**
 * Write every ticket that carries a body under `context/tickets/` and return the context the run
 * keeps: those tickets with `path` set and `body` dropped, everything else untouched. A context
 * without ticket bodies creates nothing and is returned as is. Duplicate `ref`s are coalesced
 * first (see `dedupeByRef`).
 */
export function persistContext(runDir: string, context: Context, opts: PersistOpts = {}): Context {
  const rawTickets = context.ticket ?? [];
  const withBody = new Set(
    rawTickets.filter((t) => typeof t.body === "string" && t.body.trim().length > 0),
  );
  if (withBody.size === 0) return context;
  const tickets = dedupeByRef(rawTickets);
  const now = opts.now ?? (() => new Date().toISOString());
  const fetchedAt = now();
  mkdirSync(join(contextDir(runDir), TICKETS_DIR), { recursive: true });
  const stored: ContextTicket[] = tickets.map((t) => {
    if (!withBody.has(t)) return t;
    const path = ticketFilePath(runDir, t.ref);
    writeAtomic(path, ticketMarkdown(t, fetchedAt));
    const { body: _body, ...rest } = t;
    return { ...rest, path };
  });
  writeAtomic(join(contextDir(runDir), INDEX_FILE), indexMarkdown(stored.filter((t) => t.path)));
  return { ...context, ticket: stored };
}
