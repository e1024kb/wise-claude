// Local git fixtures for the unit-pipeline tests: a bare `origin` plus a clone, no network.
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { spawnRunner } from "../../src/phases/common.ts";
import type { CommandRunner, ExecResult } from "../../src/phases/common.ts";

export type RepoPair = { root: string; origin: string; clone: string; base: string };

/** Environment for the fixture's own git calls: no user or system config, fixed identity. */
export function gitEnv(home: string): Record<string, string> {
  return {
    PATH: process.env.PATH ?? "",
    HOME: home,
    GIT_CONFIG_GLOBAL: "/dev/null",
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_TERMINAL_PROMPT: "0",
    GIT_AUTHOR_NAME: "wise-test",
    GIT_AUTHOR_EMAIL: "wise-test@example.invalid",
    GIT_COMMITTER_NAME: "wise-test",
    GIT_COMMITTER_EMAIL: "wise-test@example.invalid",
  };
}

export function git(cwd: string, args: string[], home = cwd): string {
  return execFileSync("git", args, { cwd, env: gitEnv(home), encoding: "utf8" }).trim();
}

/** Commit `content` at `rel` in `repo`; returns the new HEAD sha. */
export function commitFile(repo: string, rel: string, content: string, message: string): string {
  const path = join(repo, rel);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, content, "utf8");
  git(repo, ["add", rel]);
  git(repo, ["-c", "commit.gpgsign=false", "commit", "-q", "-m", message]);
  return git(repo, ["rev-parse", "HEAD"]);
}

/** Seed repo with one commit on `main`, cloned bare as `origin.git`, then cloned to `clone`. */
export function makeRepoPair(root: string): RepoPair {
  const seed = join(root, "seed");
  mkdirSync(seed, { recursive: true });
  git(seed, ["init", "-q", "-b", "main"], root);
  commitFile(seed, "README.md", "# fixture\n", "init");
  const origin = join(root, "origin.git");
  git(root, ["clone", "-q", "--bare", seed, origin]);
  const clone = join(root, "clone");
  git(root, ["clone", "-q", origin, clone]);
  return { root, origin, clone, base: "main" };
}

export function result(stdout: string, code = 0, stderr = ""): ExecResult {
  return { code, stdout, stderr, timedOut: false };
}

export type GhRule = (args: string[]) => ExecResult | undefined;
export type FakeExec = CommandRunner & { gh: string[][] };

/** `gh` answered by `rule` (recorded), everything else runs for real through `spawnRunner`. */
export function fakeExec(rule: GhRule = () => undefined): FakeExec {
  const calls: string[][] = [];
  const exec: CommandRunner = (cmd, args, opts) => {
    if (cmd !== "gh") return spawnRunner(cmd, args, opts);
    const argv = [...args];
    calls.push(argv);
    return Promise.resolve(rule(argv) ?? result("", 1, "gh: fake: no rule for " + argv.join(" ")));
  };
  return Object.assign(exec, { gh: calls });
}

/** Match a `gh` argv prefix. */
export function startsWith(args: string[], ...prefix: string[]): boolean {
  return prefix.every((p, i) => args[i] === p);
}
