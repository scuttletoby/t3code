import { ServerSettings } from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import { withFieldsFrom } from "./forkRowReset.logic";

const fromFile = Schema.decodeUnknownSync(ServerSettings);

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
});
