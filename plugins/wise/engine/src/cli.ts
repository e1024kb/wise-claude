// wise-engine CLI: definition commands (M1.8), daemon client commands, and the two stdio MCP
// servers (`mcp` for the harness, `unit-mcp` for a child).
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { parse as parseYaml } from "yaml";
import { defaultRoots, listDefs, loadDef, locateDef, onPath, validateDef } from "./defs.ts";
import { adapterFor, hasAdapter } from "./adapters/index.ts";
import { LOGIN_CMDS, probeOne, readyHarnesses } from "./auth.ts";
import { HARNESSES } from "./types.ts";
import type { Harness } from "./types.ts";
import { migrateDef, renderDef } from "./migrate.ts";
import type { MigrationNote } from "./migrate.ts";
import { buildQuestionary } from "./preflight.ts";
import { PROFILE_LEVELS } from "./types.ts";
import type { Context, LocatedDef, ProfileLevel, ValidationIssue } from "./types.ts";
import { buildId, runtimeName } from "./version.ts";
import { daemonCommand } from "./daemon.ts";
import { clientCommand } from "./cli-client.ts";
import { mcpCommand } from "./mcp.ts";
import { unitMcpCommand } from "./unit-mcp.ts";

const USAGE = `wise-engine <command> [options]

Commands:
  preflight <workflow> [--profile low|medium|max] [--context <json>]
                              questionary spec: {workflow, version, questions, defaults}
  compile-check <workflow>...  validate definitions; exit 1 on any error
  migrate <workflow.yaml> [--write] [--out <path>]
                               rewrite a v1 workflow as v2; dry run unless --write (in place,
                               original kept as <file>.v1.bak) or --out; exit 1 if the result
                               still has validation errors
  list-defs                    bundled and user workflow definitions
  run <workflow> [--cwd <dir>] [--answers <json>] [--context <json>] [--input k=v]
                 [--profile low|medium|max] [--follow]
                               start a run through the daemon (auto-started)
  wait|status|answer|cancel|resume|report ...
                               daemon client commands; see each command's --help
  daemon serve|start|stop|status
                               background daemon wise-engined
  mcp [--no-start]             stdio MCP server (thin daemon client; used by .mcp.json)
  unit-mcp [--token <t>]       child-side stdio MCP server (wise_report/ask/context/checkpoint);
                               token and socket from WISE_STEP_TOKEN / WISE_ENGINE_SOCKET / WISE_DATA_ROOT
  auth [harness...] [--json]   which harness CLIs are installed and logged in (subscription probe);
                               exit 1 when claude is missing or logged out
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

async function cmdPreflight(p: Parsed, io: Io): Promise<number> {
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
  const ctx: { context?: Context; profile?: ProfileLevel; harnesses: Harness[] } = {
    // Same probe the daemon runs, so this preview matches what a conductor sees.
    harnesses: await readyHarnesses(def, (h) => (hasAdapter(h) ? adapterFor(h) : undefined)),
  };
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

/**
 * `migrate <file> [--write] [--out <path>]`: rewrite a v1 workflow as v2 (M6.4). Dry run by
 * default; `--write` replaces the file after copying it to `<file>.v1.bak`, `--out` writes
 * elsewhere. Exit 0 when the result validates without errors, 1 when it does not.
 */
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
  const source = readFileSync(located.path, "utf8");
  const raw: unknown = parseYaml(source) ?? {};
  const alreadyV2 =
    typeof raw === "object" && raw !== null && (raw as { version?: unknown }).version === 2;
  const { def, notes } = migrateDef(raw, located.path);
  const yaml = renderDef(def);
  const { def: valid, issues } = validateDef(def, located.path);
  const write = p.flags.write === true;
  const outFlag = str(p.flags.out);
  const written: string[] = [];
  let backup: string | null = null;
  if (!alreadyV2) {
    if (/^\s*#/m.test(source)) {
      notes.push({
        path: "",
        kind: "manual",
        message: "comments in the source file are not carried over; copy the ones that still apply",
      });
    }
    if (write) {
      backup = `${located.path}.v1.bak`;
      if (existsSync(backup)) {
        notes.push({ path: "", kind: "warning", message: `kept the existing backup ${backup}` });
      } else copyFileSync(located.path, backup);
      writeFileSync(located.path, yaml);
      written.push(located.path);
    }
    if (outFlag !== undefined) {
      const outPath = resolve(outFlag);
      mkdirSync(dirname(outPath), { recursive: true });
      writeFileSync(outPath, yaml);
      written.push(outPath);
    }
  }
  const errors = issues.filter((i) => i.level === "error");
  const result = {
    workflow: located.name,
    path: located.path,
    already_v2: alreadyV2,
    dry_run: written.length === 0,
    ok: Boolean(valid),
    written,
    backup,
    notes,
    issues,
    yaml,
  };
  emit(io, p, result, () => {
    if (alreadyV2) return `${located.path}: already v2, nothing to migrate`;
    const count = (kind: MigrationNote["kind"]) => notes.filter((n) => n.kind === kind).length;
    const where = written.length
      ? `written to ${written.join(", ")}${backup ? ` (backup ${backup})` : ""}`
      : "dry run, nothing written (use --write or --out)";
    return [
      `${located.path}: migrated to v2, ${count("rewritten")} rewritten, ${count("warning")} warning(s), ${count("manual")} manual; ${where}`,
      ...notes.map((n) => `  ${n.kind.toUpperCase()} ${n.path}: ${n.message}`),
      errors.length
        ? `  result still has ${errors.length} validation error(s):`
        : "  result validates with no errors",
      ...errors.map(formatIssue),
    ].join("\n");
  });
  return valid ? 0 : 1;
}

function cmdListDefs(p: Parsed, io: Io): number {
  const defs = listDefs(rootsFrom(p, io));
  emit(io, p, defs, () => defs.map((d) => `${d.name}\t${d.source}\t${d.path}`).join("\n"));
  return 0;
}

type AuthRow = { harness: Harness; installed: boolean; login: "ok" | "missing"; login_cmd: string };

/**
 * `/wise-init` reads this: one row per harness with the binary on PATH and the subscription
 * login probe. Claude is required for any run, the other three are optional.
 */
async function cmdAuth(p: Parsed, io: Io): Promise<number> {
  const wanted = p.positional.length > 0 ? p.positional : [...HARNESSES];
  const rows: AuthRow[] = [];
  for (const name of wanted) {
    if (!(HARNESSES as readonly string[]).includes(name)) {
      io.err(`auth: unknown harness ${name} (one of ${HARNESSES.join(", ")})\n`);
      return 2;
    }
    const harness = name as Harness;
    const installed = onPath(harness, io.env ?? process.env);
    const probe = installed
      ? await probeOne(harness, "subscription", (h) => (hasAdapter(h) ? adapterFor(h) : undefined))
      : { ok: false, login_cmd: LOGIN_CMDS[harness] };
    rows.push({
      harness,
      installed,
      login: probe.ok ? "ok" : "missing",
      login_cmd: probe.login_cmd,
    });
  }
  if (p.flags.json) {
    io.out(JSON.stringify(rows) + "\n");
  } else {
    for (const r of rows) {
      io.out(
        `HARNESS=${r.harness} INSTALLED=${r.installed ? "yes" : "no"} LOGIN=${r.login} LOGIN_CMD=${r.login_cmd}\n`,
      );
    }
  }
  const claude = rows.find((r) => r.harness === "claude");
  return claude !== undefined && claude.login !== "ok" ? 1 : 0;
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
      case "unit-mcp":
        return await unitMcpCommand(argv.slice(1), io);
      case "daemon":
        return await daemonCommand(argv.slice(1), io);
      case "preflight":
        return await cmdPreflight(p, io);
      case "compile-check":
        return cmdCompileCheck(p, io);
      case "migrate":
        return cmdMigrate(p, io);
      case "list-defs":
        return cmdListDefs(p, io);
      case "version":
        io.out(`wise-engine ${buildId()} (${runtimeName()} ${process.versions.node})\n`);
        return 0;
      case "auth":
        return await cmdAuth(p, io);
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
