import {
  HANDOFF_MAX_TOKENS,
  HANDOFF_MIN_TOKENS,
  HANDOFF_THREADS_MAX,
  HandoffSettings,
  ServerSettings,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  boundHandoffTokens,
  formatArgvLines,
  handoffSourceProblem,
  handoffThreadRows,
  parseArgvLines,
  handoffRolesLabel,
  threadModeProblem,
  withRolesMode,
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

describe("boundHandoffTokens", () => {
  it("keeps a typed size within what the server's save takes, both ways", () => {
    const empty = fromFile({}).handoff;
    for (const typed of [0, 1, 19_999, 150_000, 10_000_001, 200_000_000]) {
      const sizeTokens = boundHandoffTokens(typed, false);
      const idleTokens = boundHandoffTokens(typed, true);
      expect(isSavable({ ...empty, sizeTokens, idleTokens }), String(typed)).toBe(true);
    }
    expect(boundHandoffTokens(200_000_000, false)).toBe(HANDOFF_MAX_TOKENS);
    expect(boundHandoffTokens(150_000, false)).toBe(150_000);
    expect(boundHandoffTokens(0, false)).toBe(HANDOFF_MIN_TOKENS);
    expect(boundHandoffTokens(0, true)).toBe(0);
    expect(boundHandoffTokens(1, true)).toBe(HANDOFF_MIN_TOKENS);
  });

  it("matches the server's limit: one token more is refused", () => {
    const empty = fromFile({}).handoff;
    expect(isSavable({ ...empty, sizeTokens: HANDOFF_MAX_TOKENS + 1 })).toBe(false);
  });
});

const decodeHandoff = Schema.decodeSync(HandoffSettings);
const isHandoffSettings = Schema.is(HandoffSettings);

describe("the role threads' mode (HO6)", () => {
  const base = decodeHandoff({ mode: "off" });

  it("sets the roles mode, and removes it for the mode above, as a save the server takes", () => {
    const set = withRolesMode(base, "handoff");
    expect(set.roles).toBe("handoff");
    expect(isHandoffSettings(set)).toBe(true);
    const back = withRolesMode(set, "inherit");
    expect(Object.hasOwn(back, "roles")).toBe(false);
    expect(back).toEqual(base);
  });

  it("labels the choice", () => {
    expect(handoffRolesLabel("inherit")).toBe("Same as above");
    expect(handoffRolesLabel("handoff")).toBe("Handoff");
  });
});
