import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  playtestCommandLine,
  playtestMissingSteps,
  playtestStepsText,
  withPlaytestSteps,
} from "./playtestSettings.logic";

const fromFile = Schema.decodeUnknownSync(ServerSettings);
const step = (...argv: string[]) => ({ argv });
const STEPS = {
  claim: step("/usr/local/bin/game", "claim"),
  renew: step("/usr/local/bin/game", "renew"),
  push: [{ name: "deploy", ...step("/usr/local/bin/game", "deploy") }],
  release: step("/usr/local/bin/game", "release"),
};

describe("playtest steps", () => {
  it("an empty settings file has no steps, so the switch names all four as missing", () => {
    const empty = fromFile({}).playtest;
    expect(playtestMissingSteps(empty)).toEqual(["claim", "renew", "push", "release"]);
    expect(playtestStepsText(empty)).toBe("{}");
  });

  it("saves the edited steps over the old ones and keeps the switch and limits", () => {
    const stored = fromFile({ playtest: { maxHours: 3 } }).playtest;
    const result = withPlaytestSteps(stored, JSON.stringify(STEPS));
    if (!("value" in result)) throw new Error(result.error);
    expect(result.value.maxHours).toBe(3);
    expect(result.value.enabled).toBe(false);
    expect(result.value.push.map((push) => push.name)).toEqual(["deploy"]);
    expect(playtestMissingSteps(result.value)).toEqual([]);
    // Shown again, the text reads back to the same value.
    expect(withPlaytestSteps(result.value, playtestStepsText(result.value))).toEqual(result);
  });

  it("a step left out is removed, but not while playtest is on", () => {
    const on = fromFile({ playtest: { enabled: true, ...STEPS } }).playtest;
    const { release: _release, ...withoutRelease } = STEPS;
    expect(withPlaytestSteps(on, JSON.stringify(withoutRelease))).toHaveProperty("error");
    const off = { ...on, enabled: false };
    const result = withPlaytestSteps(off, JSON.stringify(withoutRelease));
    expect("value" in result && result.value.release).toBeUndefined();
  });

  it("refuses text the server's save would refuse", () => {
    const empty = fromFile({}).playtest;
    for (const text of [
      "not json",
      "[]",
      JSON.stringify({ claim: step("game", "claim") }),
      JSON.stringify({ push: [{ name: "claim", ...step("/bin/x") }] }),
      JSON.stringify({ claim: { ...step("/bin/x"), typo: 1 } }),
      JSON.stringify({ enabled: true }),
    ]) {
      expect(withPlaytestSteps(empty, text), text).toHaveProperty("error");
    }
  });

  it("shows a step as one command line", () => {
    expect(
      playtestCommandLine({ argv: ["/bin/x", "a b", "c"], timeoutSeconds: 1, description: "" }),
    ).toBe('/bin/x "a b" c');
  });
});
