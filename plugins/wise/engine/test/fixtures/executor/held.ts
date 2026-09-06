// A controllable agent starter: the child stays "running" until the test finishes it, records
// nudges and kills, and exposes the event sink so tests can script a vendor stream.
import type { AgentHandle, AgentStarter } from "../../../src/steps/agent.ts";
import type { Harness, RunReq, RunRes } from "../../../src/types.ts";

export type Held = {
  harness: Harness;
  req: RunReq;
  handle: AgentHandle;
  nudges: string[];
  kills: NodeJS.Signals[];
  /** Push a scripted vendor event into the step. */
  push: (parsed: unknown) => void;
  /** End the child with this result. */
  finish: (res: RunRes) => void;
};

export function heldStarter(opts: { killResolves?: boolean } = {}): {
  starter: AgentStarter;
  held: Held[];
} {
  const held: Held[] = [];
  const starter: AgentStarter = (harness, req, onEvent) => {
    const { promise, resolve } = Promise.withResolvers<RunRes>();
    const entry: Held = {
      harness,
      req,
      nudges: [],
      kills: [],
      push: (parsed) =>
        onEvent({ ts: new Date().toISOString(), harness, line: JSON.stringify(parsed), parsed }),
      finish: (res) => resolve(res),
      handle: {
        pid: 0,
        done: promise,
        nudge: (text) => {
          entry.nudges.push(text);
        },
        kill: (signal = "SIGTERM") => {
          entry.kills.push(signal);
          if (opts.killResolves !== false) {
            resolve({
              text: "",
              usage: { input: 0, output: 0, cache_read: 0, cache_write: 0, pool: req.auth },
              exit: "error",
              error: `no result event (signal ${signal})`,
              cursor: "sess-held",
            });
          }
        },
      },
    };
    held.push(entry);
    return entry.handle;
  };
  return { starter, held };
}
