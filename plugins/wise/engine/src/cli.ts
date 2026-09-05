// wise-engine CLI (M1.8). Non-executing commands only; daemon and mcp arrive in M2.
import { existsSync, statSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { defaultRoots, listDefs, loadDef, locateDef, validateDef } from "./defs.ts";
import { buildQuestionary } from "./preflight.ts";
import { PROFILE_LEVELS } from "./types.ts";
import type { Context, LocatedDef, ProfileLevel, ValidationIssue } from "./types.ts";
import { pluginVersion, runtimeName } from "./version.ts";
import { daemonCommand } from "./daemon.ts";
import { clientCommand } from "./cli-client.ts";
import { mcpCommand } from "./mcp.ts";

const USAGE = `wise-engine <command> [options]

Commands:
  preflight <workflow> [--profile low|medium|max] [--context <json>]
                              questionary spec: {workflow, version, questions, defaults}
  compile-check <workflow>...  validate definitions; exit 1 on any error
  migrate <workflow.yaml>      dry run: list v1 constructs with their v2 replacement
  list-defs                    bundled and user workflow definitions
  run <workflow> [--cwd <dir>] [--answers <json>] [--context <json>] [--input k=v]
                 [--profile low|medium|max] [--follow]
                               start a run through the daemon (auto-started)
  wait|status|answer|cancel|resume|report ...
                               daemon client commands; see each command's --help
  daemon serve|start|stop|status
                               background daemon wise-engined
  mcp [--no-start]             stdio MCP server (thin daemon client; used by .mcp.json)
  version                      plugin version and runtime
  help                         this text

<workflow> is a definition name (user root shadows bundled) or a path to a .yaml file.
Options: --json (default) | --text   --user-root <dir>   --bundled-root <dir>
`;

export type Io = { out: (s: string) => void; err: (s: string) => void; env?: NodeJS.ProcessEnv };

type Parsed = { cmd: string; positional: string[]; flags: Record<string, string | true> };

function parseArgs(argv: readonly string[]): Parsed {
  const [cmd = "help", ...rest] = argv;
  const positional: string[] = [];
  const flags: Record<string, string | true> = {};
  for (let i = 0; i < rest.length; i++) {
    const tok = rest[i] as string;
    if (tok.startsWith("--")) {
      const eq = tok.indexOf("=");
      if (eq > 0) {
        flags[tok.slice(2, eq)] = tok.slice(eq + 1);
      } else {
        const next = rest[i + 1];
        if (next !== undefined && !next.startsWith("--")) {
          flags[tok.slice(2)] = next;
          i++;
        } else {
          flags[tok.slice(2)] = true;
        }
      }
    } else {
      positional.push(tok);
    }
  }
  return { cmd, positional, flags };
}

function str(flag: string | true | undefined): string | undefined {
  return typeof flag === "string" ? flag : undefined;
}

function rootsFrom(p: Parsed, io: Io) {
  const roots = defaultRoots({ env: io.env ?? process.env });
  const user = str(p.flags["user-root"]);
  const bundled = str(p.flags["bundled-root"]);
  if (user) roots.userRoot = user;
  if (bundled) roots.bundledRoot = bundled;
  return roots;
}

/** A name resolves through the roots; a path to an existing .yaml file is used as is. */
function locate(ref: string, p: Parsed, io: Io): LocatedDef | null {
  const asPath = resolve(ref);
  if (ref.endsWith(".yaml") || ref.endsWith(".yml") || ref.includes("/")) {
    if (!existsSync(asPath) || !statSync(asPath).isFile()) return null;
    const dir = dirname(asPath);
    const name =
      basename(asPath) === "workflow.yaml"
        ? basename(dir)
        : basename(asPath).replace(/\.ya?ml$/, "");
    return { name, path: asPath, dir, source: "user" };
  }
  return locateDef(ref, rootsFrom(p, io));
}

function formatIssue(i: ValidationIssue): string {
  const hint = i.hint ? `\n    -> ${i.hint}` : "";
  return `  ${i.level.toUpperCase()} ${i.path}: ${i.message}${hint}`;
}

function emit(io: Io, p: Parsed, data: unknown, text: () => string): void {
  io.out(p.flags.text === true ? text() + "\n" : JSON.stringify(data, null, 2) + "\n");
}

function cmdPreflight(p: Parsed, io: Io): number {
  const ref = p.positional[0];
  if (!ref) {
    io.err("preflight: missing <workflow>\n");
    return 64;
  }
  const located = locate(ref, p, io);
  if (!located) {
    io.err(`preflight: workflow not found: ${ref}\n`);
    emit(io, p, { error: { code: "WORKFLOW_NOT_FOUND", workflow: ref } }, () => "not found");
    return 2;
  }
  const { def, issues } = validateDef(loadDef(located.path), located.path);
  if (!def) {
    emit(io, p, { error: { code: "WORKFLOW_INVALID", workflow: located.name, issues } }, () =>
      [`${located.path}: invalid`, ...issues.map(formatIssue)].join("\n"),
    );
    return 1;
  }
  const profileFlag = str(p.flags.profile);
  if (profileFlag !== undefined && !(PROFILE_LEVELS as readonly string[]).includes(profileFlag)) {
    io.err(`preflight: --profile must be one of ${PROFILE_LEVELS.join("|")}\n`);
    return 64;
  }
  let context: Context | undefined;
  const ctxFlag = str(p.flags.context);
  if (ctxFlag !== undefined) {
    try {
      context = JSON.parse(ctxFlag) as Context;
    } catch (e) {
      io.err(`preflight: --context is not JSON: ${(e as Error).message}\n`);
      return 64;
    }
  }
  const ctx: { context?: Context; profile?: ProfileLevel } = {};
  if (context) ctx.context = context;
  if (profileFlag) ctx.profile = profileFlag as ProfileLevel;
  const q = buildQuestionary(def, ctx);
  const result = {
    workflow: located.name,
    version: def.version,
    questions: q.questions,
    defaults: q.defaults,
    warnings: issues.filter((i) => i.level === "warning"),
  };
  emit(io, p, result, () =>
    [
      `${located.name} v${def.version}`,
      ...q.questions.map(
        (qq) =>
          `  ${qq.id} [${qq.kind}${qq.locked ? ", locked" : ""}] ${qq.label}` +
          (qq.default !== undefined ? ` (default: ${JSON.stringify(qq.default)})` : ""),
      ),
    ].join("\n"),
  );
  return 0;
}

function cmdCompileCheck(p: Parsed, io: Io): number {
  if (p.positional.length === 0) {
    io.err("compile-check: missing <workflow>\n");
    return 64;
  }
  let failed = false;
  const report: {
    workflow: string;
    path: string | null;
    ok: boolean;
    issues: ValidationIssue[];
  }[] = [];
  for (const ref of p.positional) {
    const located = locate(ref, p, io);
    if (!located) {
      failed = true;
      report.push({
        workflow: ref,
        path: null,
        ok: false,
        issues: [{ level: "error", path: "", message: "workflow not found" }],
      });
      continue;
    }
    const { def, issues } = validateDef(loadDef(located.path), located.path);
    if (!def) failed = true;
    report.push({ workflow: located.name, path: located.path, ok: Boolean(def), issues });
  }
  emit(io, p, report, () =>
    report
      .map((r) =>
        [
          `${r.ok ? "OK" : "FAIL"} ${r.workflow}${r.path ? ` (${r.path})` : ""}`,
          ...r.issues.map(formatIssue),
        ].join("\n"),
      )
      .join("\n"),
  );
  return failed ? 1 : 0;
}

function cmdMigrate(p: Parsed, io: Io): number {
  const ref = p.positional[0];
  if (!ref) {
    io.err("migrate: missing <workflow.yaml>\n");
    return 64;
  }
  const located = locate(ref, p, io);
  if (!located) {
    io.err(`migrate: workflow not found: ${ref}\n`);
    return 2;
  }
  const { def, issues } = validateDef(loadDef(located.path), located.path);
  const hints = issues.filter((i) => i.hint);
  const result = {
    workflow: located.name,
    path: located.path,
    already_v2: Boolean(def) && hints.length === 0,
    dry_run: true,
    changes: hints.map((i) => ({ path: i.path, from: i.message, to: i.hint })),
    blocking: issues.filter((i) => i.level === "error" && !i.hint),
  };
  emit(io, p, result, () =>
    result.already_v2
      ? `${located.path}: already v2, nothing to migrate`
      : [
          `${located.path}: ${result.changes.length} change(s) (dry run, nothing written)`,
          ...result.changes.map((c) => `  ${c.path}: ${c.from}\n    -> ${c.to}`),
          ...(result.blocking.length
            ? ["  blocking (no automatic migration):", ...result.blocking.map(formatIssue)]
            : []),
        ].join("\n"),
  );
  return 0;
}

function cmdListDefs(p: Parsed, io: Io): number {
  const defs = listDefs(rootsFrom(p, io));
  emit(io, p, defs, () => defs.map((d) => `${d.name}\t${d.source}\t${d.path}`).join("\n"));
  return 0;
}

export async function main(
  argv: readonly string[],
  io: Io = { out: (s) => process.stdout.write(s), err: (s) => process.stderr.write(s) },
): Promise<number> {
  const p = parseArgs(argv);
  try {
    switch (p.cmd) {
      case "run":
      case "wait":
      case "status":
      case "answer":
      case "cancel":
      case "resume":
      case "report":
        return await clientCommand(argv, io);
      case "mcp":
        return await mcpCommand(argv.slice(1), io);
      case "daemon":
        return await daemonCommand(argv.slice(1), io);
      case "preflight":
        return cmdPreflight(p, io);
      case "compile-check":
        return cmdCompileCheck(p, io);
      case "migrate":
        return cmdMigrate(p, io);
      case "list-defs":
        return cmdListDefs(p, io);
      case "version":
        io.out(`wise-engine ${pluginVersion()} (${runtimeName()} ${process.versions.node})\n`);
        return 0;
      case "help":
      case "--help":
      case "-h":
        io.out(USAGE);
        return 0;
      default:
        io.err(`wise-engine: unknown command '${p.cmd}'\n\n${USAGE}`);
        return 64;
    }
  } catch (e) {
    io.err(`wise-engine: ${(e as Error).message}\n`);
    return 70;
  }
}

if (import.meta.main ?? process.argv[1] === new URL(import.meta.url).pathname) {
  process.exitCode = await main(process.argv.slice(2));
}
