import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { subRowResetValue, withFieldsFrom } from "./forkRowReset.logic";

const fromFile = Schema.decodeUnknownSync(ServerSettings);
const STEPS = {
  claim: { argv: ["/usr/local/bin/game", "claim"] },
  renew: { argv: ["/usr/local/bin/game", "renew"] },
  push: [{ name: "deploy", argv: ["/usr/local/bin/game", "deploy"] }],
  release: { argv: ["/usr/local/bin/game", "release"] },
};

describe("withFieldsFrom", () => {
  it("puts back only the row's own fields, and removes one the environment lacks", () => {
    const project = fromFile({
      handoff: {
        mode: "handoff",
        idleTokens: 0,
        threads: { "thread-1": "compact" },
        source: "command",
        command: ["/usr/local/bin/write"],
      },
    }).handoff;
    const environment = fromFile({ handoff: { mode: "compact" } }).handoff;

    expect(withFieldsFrom(project, environment, ["idleTokens"])).toEqual({
      ...project,
      idleTokens: environment.idleTokens,
    });
    const source = withFieldsFrom(project, environment, ["source", "command"]);
    expect(source).toEqual({ ...project, source: "agent", command: undefined });
    expect(source).not.toHaveProperty("command");
  });

  it("is null when the row already reads as the environment's", () => {
    const value = fromFile({ playtest: { renewMinutes: 5 } }).playtest;
    expect(withFieldsFrom(value, { ...value }, ["renewMinutes"])).toBeNull();
  });

  it("compares object and list fields by value, not by reference", () => {
    const handoff = fromFile({ handoff: { threads: { "thread-1": "compact" } } }).handoff;
    const sameThreads = fromFile({ handoff: { threads: { "thread-1": "compact" } } }).handoff;
    expect(sameThreads.threads).not.toBe(handoff.threads);
    expect(withFieldsFrom(handoff, sameThreads, ["threads"])).toBeNull();
    const otherThreads = fromFile({ handoff: { threads: { "thread-1": "handoff" } } }).handoff;
    expect(withFieldsFrom(handoff, otherThreads, ["threads"])).toEqual(otherThreads);

    const playtest = fromFile({ playtest: STEPS }).playtest;
    const samePush = fromFile({ playtest: STEPS }).playtest;
    expect(samePush.push).not.toBe(playtest.push);
    expect(withFieldsFrom(playtest, samePush, ["push"])).toBeNull();
    const otherPush = fromFile({
      playtest: { ...STEPS, push: [{ name: "ship", argv: ["/usr/local/bin/game", "ship"] }] },
    }).playtest;
    expect(withFieldsFrom(playtest, otherPush, ["push"])).toEqual({
      ...playtest,
      push: otherPush.push,
    });
  });
});

describe("subRowResetValue", () => {
  const steps = ["claim", "renew", "push", "release"] as const;

  it("puts back the environment's steps on a project whose playtest is off", () => {
    const project = fromFile({ playtest: STEPS }).playtest;
    const environment = fromFile({}).playtest;
    expect(subRowResetValue("playtest", project, environment, steps)).toEqual({
      ...project,
      push: [],
      claim: undefined,
      renew: undefined,
      release: undefined,
    });
  });

  it("offers no reset that the server refuses: a project's playtest on, the environment's steps missing one", () => {
    const project = fromFile({ playtest: { ...STEPS, enabled: true } }).playtest;
    const { claim: _claim, ...withoutClaim } = STEPS;
    const environment = fromFile({ playtest: withoutClaim }).playtest;
    expect(withFieldsFrom(project, environment, steps)).not.toBeNull();
    expect(subRowResetValue("playtest", project, environment, steps)).toBeNull();

    const complete = fromFile({
      playtest: { ...STEPS, push: [{ name: "ship", argv: ["/usr/local/bin/game", "ship"] }] },
    }).playtest;
    expect(subRowResetValue("playtest", project, complete, steps)).toEqual({
      ...project,
      push: complete.push,
    });
  });

  it("is null when the fields already match", () => {
    const value = fromFile({ handoff: { sizeTokens: 90_000 } }).handoff;
    expect(subRowResetValue("handoff", value, { ...value }, ["sizeTokens"])).toBeNull();
  });
});
