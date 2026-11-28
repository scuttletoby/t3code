/** Fork: the `accountOrder` setting as the file holds it and as a save sends it. */
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeServerFile = Schema.decodeUnknownSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

describe("accountOrder", () => {
  it("is empty by default, so nothing moves", () => {
    expect(decodeServerFile({}).accountOrder).toEqual([]);
  });

  it("keeps the owner's order as written", () => {
    expect(decodeServerFile({ accountOrder: ["claude_b", "claude_a"] }).accountOrder).toEqual([
      "claude_b",
      "claude_a",
    ]);
  });

  it("reads a bad file leniently: bad entries and repeats drop, a non-list reads as empty", () => {
    expect(
      decodeServerFile({ accountOrder: ["claude_a", 7, "Not An Id!", "claude_a", "claude_b"] })
        .accountOrder,
    ).toEqual(["claude_a", "claude_b"]);
    for (const value of [null, "claude_a", { claude_a: 1 }]) {
      const settings = decodeServerFile({ accountOrder: value, usagePacingGate: "advise" });
      expect(settings.accountOrder).toEqual([]);
      // The rest of the file still loads.
      expect(settings.usagePacingGate).toBe("advise");
    }
  });

  it("refuses a save with a repeat or an id that is not an instance id", () => {
    expect(decodePatch({ accountOrder: ["claude_a", "claude_b"] }).accountOrder).toEqual([
      "claude_a",
      "claude_b",
    ]);
    expect(() => decodePatch({ accountOrder: ["claude_a", "claude_a"] })).toThrow(
      /each account listed once/,
    );
    expect(() => decodePatch({ accountOrder: ["Not An Id!"] })).toThrow();
  });
});
