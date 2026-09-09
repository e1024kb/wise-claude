// Fake harness adapter for executor tests: scripted RunRes per call, in-flight counter, auth knob.
import type { Executor } from "../../../src/executor.ts";
import { fillAnswers } from "../../../src/preflight.ts";
import type { CallContext } from "../../../src/rpc.ts";
import type {
  Adapter,
  Answers,
  AuthMode,
  Context,
  Harness,
  RunReq,
  RunRes,
  Usage,
} from "../../../src/types.ts";

export type Script = (req: RunReq, call: number) => RunRes | Promise<RunRes>;

export type FakeAdapter = Adapter & {
  calls: RunReq[];
  inFlight: number;
  maxInFlight: number;
  probes: AuthMode[];
};

export type FakeOpts = {
  /** Resolve each run after this many ms (default 5). */
  delayMs?: number;
  loggedIn?: boolean;
  loginCmd?: string;
};

export function usage(input = 100, output = 10, pool: AuthMode = "subscription"): Usage {
  return { input, output, cache_read: 0, cache_write: 0, pool };
}

type Rec = Record<string, unknown>;

/** Fill every schema property with a canned value (first enum member, else `<name>-value`). */
export function schemaAnswer(req: RunReq, extra: Partial<RunRes> = {}): RunRes {
  const props = ((req.schema?.properties as Rec | undefined) ?? {}) as Record<string, Rec>;
  const json: Rec = {};
  for (const [name, spec] of Object.entries(props)) {
    const en = spec.enum;
    if (Array.isArray(en) && en.length > 0) json[name] = en[0];
    else if (spec.type === "integer" || spec.type === "number") json[name] = 1;
    else if (spec.type === "boolean") json[name] = true;
    else json[name] = `${name}-value`;
  }
  return {
    text: `answered ${Object.keys(json).join(",")}`,
    json,
    usage: usage(),
    cursor: `sess-${Object.keys(json).join("-") || "none"}`,
    exit: "ok",
    ...extra,
  };
}

export function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function fakeAdapter(id: Harness, script: Script, opts: FakeOpts = {}): FakeAdapter {
  const delay = opts.delayMs ?? 5;
  const fake: FakeAdapter = {
    id,
    calls: [],
    inFlight: 0,
    maxInFlight: 0,
    probes: [],
    probeAuth: (auth) => {
      fake.probes.push(auth);
      return Promise.resolve({
        ok: opts.loggedIn ?? true,
        login_cmd: opts.loginCmd ?? `${id} login`,
      });
    },
    run: async (req) => {
      fake.calls.push(req);
      fake.inFlight += 1;
      fake.maxInFlight = Math.max(fake.maxInFlight, fake.inFlight);
      try {
        await pause(delay);
        return await script(req, fake.calls.length);
      } finally {
        fake.inFlight -= 1;
      }
    },
    effortMap: (e) => e,
  };
  return fake;
}

export type StartParams = {
  workflow: string;
  cwd: string;
  answers?: Answers;
  context?: Context;
  inputs?: Record<string, string>;
};

/**
 * What a conductor does before `run`: walk the staged pre-flight, answering every question with
 * its default unless `params.answers` names it, until nothing is left, then start the run with
 * the full answer set. `run` refuses a pre-flight question left unanswered, so tests that are not
 * about the questionary go through here.
 */
export async function conductRun(
  exec: Executor,
  params: StartParams,
  ctx: CallContext,
): Promise<{ run_id: string; status: string }> {
  const { workflow, cwd } = params;
  let answers: Answers = { ...params.answers };
  for (let pass = 0; pass < 32; pass++) {
    const pre = await exec.handlers.preflight({ workflow, cwd, answers }, ctx);
    const filled = fillAnswers(pre.questions, answers);
    if (Object.keys(filled.answers).length === Object.keys(answers).length) break;
    answers = filled.answers;
  }
  return exec.handlers.run(
    { workflow, cwd, answers, context: params.context ?? {}, inputs: params.inputs ?? {} },
    ctx,
  );
}
