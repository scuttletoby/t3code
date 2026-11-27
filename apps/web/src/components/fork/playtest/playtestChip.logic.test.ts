// Fork (playtest F5): the thread chip's rules. Each test names the question it answers.
import type { ForkPlaytestChipSession } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import { playtestChipText, playtestEndMessage, playtestMayRun } from "./playtestChip.logic";

const session = (changes: Partial<ForkPlaytestChipSession> = {}) =>
  ({
    id: "pt_abcdefghij",
    target: "dev-1",
    note: "",
    state: "active",
    startedAt: "2026-11-27T10:00:00.000Z",
    rounds: 2,
    ...changes,
  }) as ForkPlaytestChipSession;

describe("playtest chip", () => {
  it("asks the server only where playtest can be on: the server's setting or a project's", () => {
    expect(playtestMayRun(null)).toBe(false);
    expect(playtestMayRun({})).toBe(false);
    expect(playtestMayRun({ playtest: { enabled: false } })).toBe(false);
    expect(playtestMayRun({ playtest: { enabled: true } })).toBe(true);
    expect(
      playtestMayRun({
        playtest: { enabled: false },
        projectSettingsOverrides: { a: {}, b: { playtest: { enabled: true } } },
      }),
    ).toBe(true);
  });

  it("names the target, the note and the rounds; End is offered unless it is already ending", () => {
    expect(playtestChipText(session({ note: "new jump" }))).toEqual({
      label: "Playtest: dev-1",
      hint: "A playtest on dev-1 (new jump) is on, 2 rounds so far. End releases the target; the worktree stays.",
      canEnd: true,
    });
    expect(playtestChipText(session({ state: "claiming" })).canEnd).toBe(true);
    expect(playtestChipText(session({ state: "ending" }))).toMatchObject({
      label: "Playtest: ending dev-1",
      canEnd: false,
    });
  });

  it("after End: silent when it ended, a note when it had ended already, a warning when the release failed", () => {
    expect(playtestEndMessage("ended")).toBeNull();
    expect(playtestEndMessage("nothing-to-end")?.type).toBe("info");
    expect(playtestEndMessage("release-failed")?.type).toBe("warning");
  });
});
