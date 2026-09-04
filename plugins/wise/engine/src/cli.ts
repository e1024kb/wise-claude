// wise-engine CLI. M1.8 adds preflight / compile-check / migrate; M2 adds daemon and mcp.
import { pluginVersion, runtimeName } from "./version.ts";

const USAGE = `wise-engine <command>

Commands:
  version     print plugin version and runtime
  help        this text
`;

export function main(argv: readonly string[]): number {
  const [cmd = "help"] = argv;
  switch (cmd) {
    case "version":
      process.stdout.write(
        `wise-engine ${pluginVersion()} (${runtimeName()} ${process.versions.node})\n`,
      );
      return 0;
    case "help":
    case "--help":
    case "-h":
      process.stdout.write(USAGE);
      return 0;
    default:
      process.stderr.write(`wise-engine: unknown command '${cmd}'\n\n${USAGE}`);
      return 64;
  }
}

process.exitCode = main(process.argv.slice(2));
