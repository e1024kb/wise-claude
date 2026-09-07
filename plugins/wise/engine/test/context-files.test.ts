import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  UNTRUSTED_NOTE,
  persistContext,
  ticketFilePath,
  ticketMarkdown,
} from "../src/context-files.ts";

function scratch(): string {
  return mkdtempSync(join(tmpdir(), "wise-context-"));
}

test("persistContext: a context without ticket bodies creates nothing and is returned as is", () => {
  const dir = scratch();
  try {
    const ctx = {
      guidance: "g",
      ticket: [
        { ref: "A-1", title: "T" },
        { ref: "A-2", body: "  " },
      ],
    };
    assert.equal(persistContext(dir, ctx), ctx);
    assert.equal(existsSync(join(dir, "context")), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("persistContext: bodies become context/tickets/<ref>.md, the state keeps ref/title/url/path", () => {
  const dir = scratch();
  try {
    const ctx = {
      guidance: "keep",
      ticket: [
        {
          ref: "LEC-772",
          title: "Fix it",
          url: "https://linear.app/x/issue/LEC-772",
          body: "## Description\nDo it.\n",
        },
        { ref: "ABC/1", body: "second" },
        { ref: "NOBODY-3", title: "No body" },
      ],
    };
    const out = persistContext(dir, ctx, { now: () => "2026-09-07T00:00:00.000Z" });
    const first = ticketFilePath(dir, "LEC-772");
    const second = ticketFilePath(dir, "ABC/1");
    assert.equal(second, join(dir, "context", "tickets", "ABC%2F1.md"));
    assert.deepEqual(out, {
      guidance: "keep",
      ticket: [
        { ref: "LEC-772", title: "Fix it", url: "https://linear.app/x/issue/LEC-772", path: first },
        { ref: "ABC/1", path: second },
        { ref: "NOBODY-3", title: "No body" },
      ],
    });
    assert.equal(
      readFileSync(first, "utf8"),
      [
        "---",
        'ref: "LEC-772"',
        'title: "Fix it"',
        'url: "https://linear.app/x/issue/LEC-772"',
        'fetched_at: "2026-09-07T00:00:00.000Z"',
        "source: conductor",
        "---",
        "",
        "# LEC-772: Fix it",
        "",
        UNTRUSTED_NOTE,
        "",
        "## Description",
        "Do it.",
        "",
      ].join("\n"),
    );
    assert.equal(
      readFileSync(second, "utf8"),
      ticketMarkdown(ctx.ticket[1]!, "2026-09-07T00:00:00.000Z"),
    );
    const index = readFileSync(join(dir, "context", "index.md"), "utf8");
    assert.match(
      index,
      /- LEC-772: Fix it \(https:\/\/linear\.app\/x\/issue\/LEC-772\) -> tickets\/LEC-772\.md/,
    );
    assert.match(index, /- ABC\/1: -> tickets\/ABC%2F1\.md/);
    assert.equal(index.includes("NOBODY-3"), false);
    // The context passed in is not mutated.
    assert.equal(ctx.ticket[0]!.body, "## Description\nDo it.\n");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("persistContext: a duplicate ref is coalesced, last body wins, one returned entry", () => {
  const dir = scratch();
  try {
    const ctx = {
      ticket: [
        { ref: "LEC-772", title: "First", body: "first body" },
        { ref: "LEC-772", title: "Second", body: "second body" },
      ],
    };
    const out = persistContext(dir, ctx, { now: () => "2026-09-07T00:00:00.000Z" });
    const path = ticketFilePath(dir, "LEC-772");
    assert.deepEqual(out, { ticket: [{ ref: "LEC-772", title: "Second", path }] });
    assert.match(readFileSync(path, "utf8"), /second body/);
    assert.equal(readFileSync(path, "utf8").includes("first body"), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
