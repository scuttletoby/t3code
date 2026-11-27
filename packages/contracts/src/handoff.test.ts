import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  DEFAULT_HANDOFF_SETTINGS,
  HANDOFF_THREADS_MAX,
  HandoffSettings,
  holdsThreadRole,
  resolveHandoffSettings,
} from "./handoff.ts";
import { FORK_AGENT_WRITABLE_KEYS, FORK_PERSON_ONLY_KEYS } from "./forkSettings.ts";
import { ProjectSettingsOverrides, ServerSettings, ServerSettingsPatch } from "./settings.ts";
import { T3ProjectFile } from "./t3ProjectFile.ts";

const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const decodeProject = Schema.decodeUnknownSync(ProjectSettingsOverrides);
const decodeHandoff = Schema.decodeSync(HandoffSettings);
const readFile = (handoff: unknown) =>
  decodeServer(handoff === undefined ? {} : { handoff }).handoff;
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeRepoFile = Schema.decodeUnknownSync(T3ProjectFile);
const ON = { mode: "handoff" } as const;

describe("handoff settings", () => {
  it("defaults to off, no source, a 150K backstop and a 75K idle size", () => {
    expect(DEFAULT_HANDOFF_SETTINGS).toEqual({
      mode: "off",
      threads: {},
      // Not `agent`: settings saved without a source start no write turns (they read as before).
      source: "none",
      instructions: "",
      sizeTokens: 150_000,
      idleTokens: 75_000,
    });
  });

  it("reads old settings without the key as off", () => {
    expect(readFile(undefined)).toEqual(DEFAULT_HANDOFF_SETTINGS);
    expect(decodeProject({}).handoff).toBeUndefined();
  });

  it("reads a handoff saved without a source as `none`; `agent` only when it is set", () => {
    // Old settings: `mode: handoff` alone keeps its handoffs (parts 1, 3 and 4), no write turns.
    expect(readFile(ON).source).toBe("none");
    expect(readFile({ ...ON, source: "agent" }).source).toBe("agent");
    // A save writes the value sparse, defaults left out: a chosen `agent` stays in the file.
    const encode = Schema.encodeSync(HandoffSettings);
    expect(encode(decodeHandoff({ ...ON, source: "agent" }))).toMatchObject({ source: "agent" });
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

  it.each([
    ["a field from a newer build", { ...ON, cacheWarm: true }],
    ["a typo", { ...ON, sizeToken: 100_000 }],
    [
      "a newer field in the model",
      { ...ON, modelSelection: { instanceId: "a", model: "x", newer: 1 } },
    ],
  ])("reads %s as off; a save refuses it", (_name, value) => {
    expect(readFile(value)).toEqual(DEFAULT_HANDOFF_SETTINGS);
    expect(() => decodePatch({ handoff: value })).toThrow();
    expect(() => decodePatch({ projectSettingsOverrides: { p: { handoff: value } } })).toThrow();
  });

  it("takes the most thread modes allowed", () => {
    const threads = threadModes(HANDOFF_THREADS_MAX);
    expect(Object.keys(readFile({ ...ON, threads }).threads)).toHaveLength(HANDOFF_THREADS_MAX);
    expect(decodePatch({ handoff: { ...ON, threads } }).handoff?.threads).toEqual(threads);
  });

  it("resolves a thread: its own mode (project, then server), else its project's value whole", () => {
    const server = decodeHandoff({ ...ON, threads: { t1: "compact", t2: "off" } });
    const project = decodeHandoff({ mode: "off", threads: { t2: "handoff" } });
    expect(resolveHandoffSettings(server, undefined, "t1").mode).toBe("compact");
    expect(resolveHandoffSettings(server, undefined, "t3").mode).toBe("handoff");
    // The project's value replaces the server's; a thread entry still wins over its `mode`.
    expect(resolveHandoffSettings(server, project, "t1")).toEqual({
      settings: project,
      mode: "compact",
    });
    expect(resolveHandoffSettings(server, project, "t2").mode).toBe("handoff");
    expect(resolveHandoffSettings(server, project, "t3").mode).toBe("off");
    // A thread id that no longer exists is never read; an inherited key name is no thread.
    expect(resolveHandoffSettings(server, null, "toString").mode).toBe("handoff");
  });

  it("gives a thread holding a role the roles mode, below its own mode (HO6)", () => {
    const server = decodeHandoff({ mode: "off", roles: "handoff", threads: { t1: "compact" } });
    expect(resolveHandoffSettings(server, undefined, "t2", true).mode).toBe("handoff");
    expect(resolveHandoffSettings(server, undefined, "t2", false).mode).toBe("off");
    // A thread's own mode still wins.
    expect(resolveHandoffSettings(server, undefined, "t1", true).mode).toBe("compact");
    // A project's value replaces the server's whole, `roles` too: not set there, its `mode`.
    const project = decodeHandoff({ mode: "compact" });
    expect(resolveHandoffSettings(server, project, "t2", true).mode).toBe("compact");
    // Old settings without `roles` read as before.
    expect(DEFAULT_HANDOFF_SETTINGS.roles).toBeUndefined();
    expect(() => decodeHandoff({ roles: "sometimes" })).toThrow();
  });

  it("knows the assistant and a project's own orchestrator as holding a role (HO6)", () => {
    const roles = { assistant: "t-assistant", orchestrators: { "p-1": "t-orch" } };
    expect(holdsThreadRole(roles, { id: "t-assistant", projectId: "p-9" })).toBe(true);
    expect(holdsThreadRole(roles, { id: "t-orch", projectId: "p-1" })).toBe(true);
    // Another project's orchestrator pointer is no role here; nor an inherited key name.
    expect(holdsThreadRole(roles, { id: "t-orch", projectId: "p-2" })).toBe(false);
    expect(holdsThreadRole(roles, { id: "t-other", projectId: "toString" })).toBe(false);
    expect(
      holdsThreadRole({ assistant: null, orchestrators: {} }, { id: "t", projectId: "p" }),
    ).toBe(false);
  });

  it("takes the command from the person's settings only, never an agent or a repository file", () => {
    const fromCommand = { ...ON, source: "command", command: ["print-state", "--json"] };
    // The command is a field of `handoff`, a person-only key: no T3 tool's input may name it
    // (personOnlySettings.test.ts), and no agent may write it.
    expect(FORK_PERSON_ONLY_KEYS).toContain("handoff");
    expect(FORK_AGENT_WRITABLE_KEYS).not.toContain("handoff");
    expect(decodePatch({ handoff: fromCommand }).handoff?.command).toEqual(fromCommand.command);
    // A repository's t3.json has no `handoff`: one written there is never read.
    expect("handoff" in T3ProjectFile.fields).toBe(false);
    const repoFile = decodeRepoFile({ handoff: fromCommand });
    expect("handoff" in repoFile).toBe(false);
    // `command` without a command is refused, never run as an empty program.
    expect(() => decodePatch({ handoff: { ...ON, source: "command" } })).toThrow();
  });
});

function threadModes(count: number): Record<string, "handoff"> {
  return Object.fromEntries(
    Array.from({ length: count }, (_, index) => [`thread-${index}`, "handoff"]),
  );
}
