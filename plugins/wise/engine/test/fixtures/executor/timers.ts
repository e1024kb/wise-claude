// Manual clock and timer queue for executor tests: `advance(ms)` fires what is due, in order.
import type { ChannelTimers } from "../../../src/channel.ts";

export type FakeTimers = ChannelTimers & { advance: (ms: number) => void; pending: () => number };

export function fakeTimers(start = 1_000_000): FakeTimers {
  let now = start;
  type Entry = { at: number; fn: () => void; cancelled: boolean };
  const queue: Entry[] = [];
  return {
    now: () => now,
    setTimeout: (fn, ms) => {
      const e: Entry = { at: now + ms, fn, cancelled: false };
      queue.push(e);
      return e;
    },
    clearTimeout: (h) => {
      (h as Entry).cancelled = true;
    },
    advance(ms) {
      const target = now + ms;
      for (;;) {
        const due = queue
          .filter((e) => !e.cancelled && e.at <= target)
          .toSorted((a, b) => a.at - b.at)[0];
        if (!due) break;
        queue.splice(queue.indexOf(due), 1);
        now = due.at;
        due.fn();
      }
      now = target;
    },
    pending: () => queue.filter((e) => !e.cancelled).length,
  };
}
