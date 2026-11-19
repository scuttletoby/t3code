import { HANDOFF_THREADS_MAX, HandoffSettings, ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  formatArgvLines,
  handoffSourceProblem,
  handoffThreadRows,
  parseArgvLines,
  threadModeProblem,
  withThreadMode,
} from "./handoffSettings.logic";

const fromFile = Schema.decodeUnknownSync(ServerSettings);
// What a save sends must pass the server's strict check (no unknown or bad field).
const isSavable = Schema.is(HandoffSettings);

describe("per-thread handoff modes", () => {
  it("adds, changes and removes one thread's mode, keeping the rest of the value", () => {
    const empty = fromFile({}).handoff;
    const one = withThreadMode(empty, "thread-1", "handoff");
    expect(one).toEqual({ ...empty, threads: { "thread-1": "handoff" } });
    expect(withThreadMode(one, "thread-1", "compact").threads).toEqual({ "thread-1": "compact" });
    expect(withThreadMode(one, "thread-1", null)).toEqual(empty);
    expect(isSavable(one)).toBe(true);
  });

  it("refuses a blank id, a thread that already has a mode, and one past the cap", () => {
    const stored = fromFile({ handoff: { threads: { "thread-1": "handoff" } } }).handoff;
    expect(threadModeProblem(stored, "  ")).toBe("Enter a thread id.");
    expect(threadModeProblem(stored, " thread-1 ")).toBe("This thread already has its own mode.");
    expect(threadModeProblem(stored, "thread-2")).toBeNull();
    const full = {
      ...stored,
      threads: Object.fromEntries(
        Array.from({ length: HANDOFF_THREADS_MAX }, (_, i) => [`t${i}`, "compact" as const]),
      ),
    };
    expect(threadModeProblem(full, "one-more")).toMatch(/^At most 500 threads/);
  });

  it("titles the threads this client knows, then lists the unknown ones (a deleted thread)", () => {
    expect(
      handoffThreadRows(
        { gone: "handoff", b: "compact", a: "handoff" },
        new Map([
          ["a", "Zebra"],
          ["b", "Apple"],
        ]),
      ).map((row) => [row.threadId, row.title]),
    ).toEqual([
      ["b", "Apple"],
      ["a", "Zebra"],
      ["gone", null],
    ]);
  });
});

describe("handoff command", () => {
  it("reads one argument per line and shows it back the same way", () => {
    const argv = parseArgvLines("  /usr/bin/write-handoff\n\n--thread \n");
    expect(argv).toEqual(["/usr/bin/write-handoff", "--thread"]);
    expect(parseArgvLines(formatArgvLines(argv))).toEqual(argv);
    expect(parseArgvLines(" \n ")).toBeUndefined();
    expect(formatArgvLines(fromFile({}).handoff.command)).toBe("");
  });

  it("refuses a command source without a command, as the server's save does", () => {
    expect(handoffSourceProblem("command", undefined)).not.toBeNull();
    expect(handoffSourceProblem("command", ["/bin/x"])).toBeNull();
    expect(handoffSourceProblem("agent", undefined)).toBeNull();
  });
});
