import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  DEFAULT_HANDOFF_SETTINGS,
  HANDOFF_THREADS_MAX,
  HandoffSettings,
  resolveHandoffSettings,
} from "./handoff.ts";
import { ProjectSettingsOverrides, ServerSettings, ServerSettingsPatch } from "./settings.ts";

const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const decodeProject = Schema.decodeUnknownSync(ProjectSettingsOverrides);
const decodeHandoff = Schema.decodeSync(HandoffSettings);
const readFile = (handoff: unknown) =>
  decodeServer(handoff === undefined ? {} : { handoff }).handoff;
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const ON = { mode: "handoff" } as const;

describe("handoff settings", () => {
  it("defaults to off, the agent as source, a 150K backstop and a 75K idle size", () => {
    expect(DEFAULT_HANDOFF_SETTINGS).toEqual({
      mode: "off",
      threads: {},
      source: "agent",
      instructions: "",
      sizeTokens: 150_000,
      idleTokens: 75_000,
    });
  });

  it("reads old settings without the key as off", () => {
    expect(readFile(undefined)).toEqual(DEFAULT_HANDOFF_SETTINGS);
    expect(decodeProject({}).handoff).toBeUndefined();
  });

  it("reads a good value as written", () => {
    const value = {
      mode: "off",
      threads: { "thread-1": "handoff", "thread-2": "compact" },
      source: "command",
      command: ["/usr/local/bin/state", "--print"],
      idleTokens: 0,
      modelSelection: { instanceId: "claude-a", model: "opus" },
    };
    expect(readFile(value)).toMatchObject(value);
  });

  it.each([
    ["a mode no build knows", { mode: "handof" }],
    ["a bad thread mode", { ...ON, threads: { "thread-1": "on" } }],
    ["a command source without a command", { ...ON, source: "command" }],
    ["a relative size", { ...ON, sizeTokens: "150K" }],
    ["a size under the handoff floor", { ...ON, sizeTokens: 5_000 }],
    ["an idle size under the floor", { ...ON, idleTokens: 5 }],
    ["a list, not an object", [ON]],
    ["too many thread modes", { ...ON, threads: threadModes(HANDOFF_THREADS_MAX + 1) }],
  ])("reads %s as off, never as handoff, and a save refuses it", (_name, value) => {
    expect(readFile(value)).toEqual(DEFAULT_HANDOFF_SETTINGS);
    expect(
      decodeServer({
        projectSettingsOverrides: { p: { handoff: value } },
      }).projectSettingsOverrides["p" as never]?.handoff,
    ).toEqual(DEFAULT_HANDOFF_SETTINGS);
    expect(() => decodePatch({ handoff: value })).toThrow();
  });

  it("reads a field from a newer build as off; a save keeps only the fields it knows", () => {
    for (const value of [
      { ...ON, cacheWarm: true },
      { ...ON, modelSelection: { instanceId: "a", model: "x", newer: 1 } },
    ]) {
      expect(readFile(value)).toEqual(DEFAULT_HANDOFF_SETTINGS);
      expect(decodePatch({ handoff: value }).handoff).not.toHaveProperty("cacheWarm");
    }
  });

  it("takes the most thread modes allowed", () => {
    const threads = threadModes(HANDOFF_THREADS_MAX);
    expect(Object.keys(readFile({ ...ON, threads }).threads)).toHaveLength(HANDOFF_THREADS_MAX);
    expect(decodePatch({ handoff: { ...ON, threads } }).handoff?.threads).toEqual(threads);
  });

  it("resolves a thread: its own mode, else its project's value whole, else the server's", () => {
    const server = decodeHandoff({ ...ON, threads: { t1: "compact" } });
    const project = decodeHandoff({ mode: "off", threads: { t2: "handoff" } });
    expect(resolveHandoffSettings(server, undefined, "t1").mode).toBe("compact");
    expect(resolveHandoffSettings(server, undefined, "t3").mode).toBe("handoff");
    // The project replaces the server's whole: the server's thread modes no longer apply.
    expect(resolveHandoffSettings(server, project, "t1")).toEqual({
      settings: project,
      mode: "off",
    });
    expect(resolveHandoffSettings(server, project, "t2").mode).toBe("handoff");
    // A thread id that no longer exists is never read; an inherited key name is no thread.
    expect(resolveHandoffSettings(server, null, "toString").mode).toBe("handoff");
  });
});

function threadModes(count: number): Record<string, "handoff"> {
  return Object.fromEntries(
    Array.from({ length: count }, (_, index) => [`thread-${index}`, "handoff"]),
  );
}
