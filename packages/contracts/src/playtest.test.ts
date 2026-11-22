/// <reference types="vite-plus/client" />

import * as Effect from "effect/Effect";
import * as Logger from "effect/Logger";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "@effect/vitest";

import { ProjectId } from "./baseSchemas.ts";
import {
  DEFAULT_PLAYTEST_SETTINGS,
  PLAYTEST_ENV,
  PLAYTEST_EXIT_OK,
  PLAYTEST_EXIT_REFUSED,
  PLAYTEST_PUSH_STEPS_MAX,
  PlaytestNote,
  PlaytestSessionId,
  PlaytestSessionRecord,
  PlaytestSettings,
  PlaytestSettingsFromSettingsFile,
  PlaytestTarget,
  isPlaytestProgramFullyQualified,
} from "./playtest.ts";
import { ServerSettings, ServerSettingsPatch } from "./settings.ts";
// The adapter contract page: the names and exit codes adapters are written from.
import PLAYTEST_DOC from "../../../docs/fork/playtest.md?raw";

const decodeStrict = Schema.decodeUnknownSync(PlaytestSettings);
const decodeFile = Schema.decodeUnknownSync(PlaytestSettingsFromSettingsFile);
const decodeFileEffect = Schema.decodeUnknownEffect(PlaytestSettingsFromSettingsFile);
const decodeServer = Schema.decodeUnknownSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const PROJECT = ProjectId.make("project-1");

/** A complete adapter as a person writes it. */
const ADAPTER = {
  enabled: true,
  claim: { argv: ["/usr/local/bin/game", "playtest", "claim"] },
  renew: { argv: ["/usr/local/bin/game", "playtest", "renew"], timeoutSeconds: 30 },
  push: [
    { name: "guard", argv: ["/usr/local/bin/game", "playtest", "guard"] },
    { name: "build", argv: ["/usr/local/bin/game", "playtest", "build"], timeoutSeconds: 1800 },
    { name: "deploy", argv: ["/usr/local/bin/game", "playtest", "deploy"], timeoutSeconds: 300 },
  ],
  release: { argv: ["/usr/local/bin/game", "playtest", "release"], timeoutSeconds: 600 },
};
const step = (argv: ReadonlyArray<string>) => ({ argv });
const without = (key: string) =>
  Object.fromEntries(Object.entries(ADAPTER).filter(([name]) => name !== key));

describe("playtest settings", () => {
  it("is off with no steps by default", () => {
    expect(DEFAULT_PLAYTEST_SETTINGS).toEqual({
      enabled: false,
      push: [],
      renewMinutes: 10,
      idleMinutes: 90,
      maxHours: 6,
    });
    expect(decodeServer({}).playtest).toEqual(DEFAULT_PLAYTEST_SETTINGS);
    // A project without its own value inherits the server's.
    expect(decodeServer({}).projectSettingsOverrides).toEqual({});
  });

  it("takes a complete adapter, with every step default filled in", () => {
    const settings = decodeStrict(ADAPTER);
    expect(settings.enabled).toBe(true);
    expect(settings.claim).toEqual({
      argv: ["/usr/local/bin/game", "playtest", "claim"],
      timeoutSeconds: 120,
      description: "",
    });
    expect(settings.push.map((push) => push.name)).toEqual(["guard", "build", "deploy"]);
    // Off with only some steps is fine: steps can be written before the switch is turned on.
    expect(decodeStrict({ claim: ADAPTER.claim }).enabled).toBe(false);
  });

  it("refuses on without all four steps", () => {
    for (const key of ["claim", "renew", "release"]) {
      expect(() => decodeStrict(without(key)), key).toThrow(/claim, renew, push and release/);
    }
    expect(() => decodeStrict({ ...ADAPTER, push: [] })).toThrow(/claim, renew, push and release/);
  });

  it("refuses a program that is not an absolute path", () => {
    for (const program of ["game", "./scripts/guard", "scripts/guard", "../bin/guard", "~/guard"]) {
      expect(() => decodeStrict({ ...ADAPTER, claim: step([program]) }), program).toThrow(
        /absolute path/,
      );
      expect(
        () => decodeStrict({ ...ADAPTER, push: [{ name: "guard", argv: [program] }] }),
        program,
      ).toThrow(/absolute path/);
    }
    for (const program of [
      "/opt/game/guard",
      "C:\\game\\guard.exe",
      "D:/game/guard",
      "\\\\host\\g",
    ]) {
      expect(decodeStrict({ ...ADAPTER, claim: step([program]) }).claim?.argv[0]).toBe(program);
    }
    expect(() => decodeStrict({ ...ADAPTER, claim: step([]) })).toThrow();
  });

  it("takes a program as fully qualified only on the server's own OS", () => {
    const cases: ReadonlyArray<readonly [string, boolean, boolean]> = [
      // program, on Linux/macOS, on Windows
      ["/opt/game/guard", true, false],
      // Drive-relative on Windows: the drive of the working folder, the agent's worktree.
      ["\\opt\\guard", false, false],
      ["C:\\game\\guard.exe", false, true],
      ["D:/game/guard", false, true],
      ["\\\\host\\share\\g", false, true],
      ["//host/share/g", true, true],
      ["\\\\?\\C:\\game\\g", false, true],
      ["c:guard", false, false],
      ["guard", false, false],
      ["./guard", false, false],
      ["", false, false],
    ];
    for (const [program, posix, windows] of cases) {
      expect(isPlaytestProgramFullyQualified(program, "linux"), program).toBe(posix);
      expect(isPlaytestProgramFullyQualified(program, "darwin"), program).toBe(posix);
      expect(isPlaytestProgramFullyQualified(program, "win32"), program).toBe(windows);
    }
  });

  it("refuses repeated, badly named or too many push steps", () => {
    const guard = ADAPTER.push[0]!;
    expect(() => decodeStrict({ ...ADAPTER, push: [guard, guard] })).toThrow(/used once/);
    expect(() => decodeStrict({ ...ADAPTER, push: [{ ...guard, name: "Guard Step" }] })).toThrow();
    // T3_PLAYTEST_STEP names the step; a push step cannot pass for claim, renew or release.
    for (const name of ["claim", "renew", "release"]) {
      expect(() => decodeStrict({ ...ADAPTER, push: [{ ...guard, name }] }), name).toThrow();
    }
    const many = Array.from({ length: PLAYTEST_PUSH_STEPS_MAX + 1 }, (_, index) => ({
      ...guard,
      name: `step-${index}`,
    }));
    expect(() => decodeStrict({ ...ADAPTER, push: many })).toThrow();
    expect(decodeStrict({ ...ADAPTER, push: many.slice(1) }).push).toHaveLength(
      PLAYTEST_PUSH_STEPS_MAX,
    );
  });

  it("refuses limits out of range", () => {
    for (const bad of [
      { renewMinutes: 0 },
      { renewMinutes: 61 },
      { idleMinutes: 4 },
      { maxHours: 0 },
      { maxHours: 49 },
      { claim: { argv: ["/bin/true"], timeoutSeconds: 0 } },
      { claim: { argv: ["/bin/true"], timeoutSeconds: 3601 } },
      { claim: { argv: ["/bin/true", ""] } },
      { claim: { argv: ["/bin/true", "  "] } },
      { claim: { argv: ["/bin/true", ...Array.from({ length: 50 }, () => "x")] } },
    ]) {
      expect(() => decodeStrict({ ...ADAPTER, ...bad }), JSON.stringify(bad)).toThrow();
    }
  });
});

describe("playtest settings in the settings file", () => {
  it("reads a bad value as off, never as on with a missing or bad step", () => {
    for (const bad of [
      without("release"),
      { ...ADAPTER, claim: step(["game"]) },
      { ...ADAPTER, push: [ADAPTER.push[0], ADAPTER.push[0]] },
      { ...ADAPTER, enabled: "yes" },
      // A newer build's field (a limit this build would not apply), at the top or in a step.
      { ...ADAPTER, onlyBranches: ["playtest/*"] },
      { ...ADAPTER, claim: { ...ADAPTER.claim, maxRuns: 1 } },
      "on",
      null,
      [],
    ]) {
      expect(decodeFile(bad), JSON.stringify(bad)).toEqual(DEFAULT_PLAYTEST_SETTINGS);
    }
    expect(decodeFile(ADAPTER)).toEqual(decodeStrict(ADAPTER));
  });

  it("keeps every other setting when the playtest value is bad", () => {
    const settings = decodeServer({
      enableAgentBrowserAccess: false,
      playtest: without("claim"),
      waitChecks: { "ci-green": { argv: ["/bin/true"] } },
    });
    expect(settings.playtest).toEqual(DEFAULT_PLAYTEST_SETTINGS);
    expect(settings.enableAgentBrowserAccess).toBe(false);
    expect(Object.keys(settings.waitChecks)).toEqual(["ci-green"]);
  });

  it("reads a project's bad value as off for that project, not as the server's adapter", () => {
    const settings = decodeServer({
      playtest: ADAPTER,
      projectSettingsOverrides: { [PROJECT]: { playtest: without("renew") } },
    });
    expect(settings.playtest.enabled).toBe(true);
    expect(settings.projectSettingsOverrides[PROJECT]?.playtest).toEqual(DEFAULT_PLAYTEST_SETTINGS);
  });

  it.effect("warns when it drops a bad value, naming an unknown field", () =>
    Effect.gen(function* () {
      const logged: Array<unknown> = [];
      const logger = Logger.make(({ message }) => logged.push(message));
      const drop = (bad: unknown) =>
        decodeFileEffect(bad).pipe(
          Effect.provide(Logger.layer([logger], { mergeWithExisting: false })),
        );
      yield* drop(without("claim"));
      expect(JSON.stringify(logged)).toContain("invalid playtest settings read as off");
      yield* drop({ ...ADAPTER, claim: { ...ADAPTER.claim, maxRuns: 1 } });
      expect(String(logged.at(-1))).toContain(
        'Expected no excess property at ["claim"]["maxRuns"]',
      );
      expect(String(logged.at(-1))).not.toMatch(/\s{2}/);
      yield* drop({
        ...ADAPTER,
        claim: step(["game", "--token=SECRETVALUE"]),
      });
      expect(String(logged.at(-1))).toContain("invalid playtest settings read as off");
      expect(String(logged.at(-1))).not.toContain("SECRETVALUE");
    }),
  );

  it("writes a decoded value back as the same value (a save, then a read, changes nothing)", () => {
    const encode = Schema.encodeSync(PlaytestSettingsFromSettingsFile);
    const once = decodeFile(ADAPTER);
    expect(decodeFile(encode(once))).toEqual(once);
    expect(decodeFile(encode(decodeFile(encode(once))))).toEqual(once);
  });
});

describe("playtest settings in a save", () => {
  it("refuses a bad server or project value", () => {
    expect(decodePatch({ playtest: ADAPTER }).playtest?.enabled).toBe(true);
    expect(() => decodePatch({ playtest: without("claim") })).toThrow();
    expect(() => decodePatch({ playtest: { ...ADAPTER, claim: step(["./guard"]) } })).toThrow();
    const project = (playtest: unknown) =>
      decodePatch({ projectSettingsOverrides: { [PROJECT]: { playtest } } });
    expect(project(ADAPTER).projectSettingsOverrides?.[PROJECT]?.playtest?.enabled).toBe(true);
    expect(() => project(without("release"))).toThrow();
    expect(() => project({ ...ADAPTER, push: [{ name: "x", argv: ["guard"] }] })).toThrow();
  });

  it.each([
    ["a field from a newer build", { ...ADAPTER, onlyBranches: ["playtest/*"] }],
    ["a typo", { ...ADAPTER, timeoutSecond: 30 }],
    ["a newer field in a step", { ...ADAPTER, claim: { ...ADAPTER.claim, maxRuns: 1 } }],
  ])("refuses %s, never drops it and saves the rest as on", (_name, value) => {
    expect(() => decodePatch({ playtest: value })).toThrow();
    expect(() =>
      decodePatch({ projectSettingsOverrides: { [PROJECT]: { playtest: value } } }),
    ).toThrow();
  });
});

describe("playtest adapter contract", () => {
  it("matches the contract page adapters are written from (docs/fork/playtest.md)", () => {
    // The variable table's rows.
    const documented = [...PLAYTEST_DOC.matchAll(/^\| `(T3_PLAYTEST_[A-Z_]+)`/gmu)].map(
      (m) => m[1],
    );
    expect(documented.toSorted()).toEqual(Object.values(PLAYTEST_ENV).toSorted());
    const exits = new Set([...PLAYTEST_DOC.matchAll(/\bExit (\d+)\b/gu)].map((m) => Number(m[1])));
    expect([...exits].toSorted()).toEqual([PLAYTEST_EXIT_OK, PLAYTEST_EXIT_REFUSED].toSorted());
    expect(PLAYTEST_DOC).toContain(`At most ${PLAYTEST_PUSH_STEPS_MAX} push steps`);
  });

  it("keeps refused apart from ok and from the usual failure codes", () => {
    expect(PLAYTEST_EXIT_OK).toBe(0);
    // 1 and 2 are what most programs and argument parsers exit with on any error; 124 is timeout's.
    expect([0, 1, 2, 124, 126, 127]).not.toContain(PLAYTEST_EXIT_REFUSED);
  });

  it("takes only plain session ids and targets", () => {
    const isId = Schema.is(PlaytestSessionId);
    expect(isId("pt_abcdefgh23")).toBe(true);
    for (const bad of ["pt_ABCDEFGH23", "w_abcdefgh23", "pt_abc", "pt_abcdefgh231"]) {
      expect(isId(bad), bad).toBe(false);
    }
    const isTarget = Schema.is(PlaytestTarget);
    for (const good of ["tutorial", "content-tutorial", "Tutorial_2", "a.b"]) {
      expect(isTarget(good), good).toBe(true);
    }
    for (const bad of ["", "-rf", ".hidden", "a b", "a/b", "a;b", "x".repeat(65)]) {
      expect(isTarget(bad), bad).toBe(false);
    }
  });
});

describe("playtest session records", () => {
  const { enabled: _enabled, ...adapter } = decodeStrict(ADAPTER);
  const record = {
    id: "pt_abcdefgh23",
    threadId: "thread-1",
    projectId: "project-1",
    target: "tutorial",
    note: "the new shop",
    worktree: "/srv/work/tutorial",
    adapter,
    state: "active",
    startedAt: "2026-11-17T10:00:00.000Z",
    lastActivityAt: "2026-11-17T10:00:00.000Z",
    rounds: 1,
    lastRound: {
      number: 1,
      state: "done",
      step: "deploy",
      detail: "",
      startedAt: "2026-11-17T10:00:00.000Z",
      endedAt: "2026-11-17T10:05:00.000Z",
    },
  };
  const decodeRecord = Schema.decodeUnknownSync(PlaytestSessionRecord);

  it("keeps the steps a session started with", () => {
    const decoded = decodeRecord(record);
    expect(decoded.adapter.push.map((step) => step.name)).toEqual(["guard", "build", "deploy"]);
    expect(decoded.adapter.release.timeoutSeconds).toBe(600);
  });

  it("refuses an adapter without steps, a round 0 and a note on two lines", () => {
    expect(() => decodeRecord({ ...record, adapter: { ...adapter, push: [] } })).toThrow();
    expect(() =>
      decodeRecord({ ...record, adapter: { ...adapter, release: undefined } }),
    ).toThrow();
    expect(() =>
      decodeRecord({ ...record, lastRound: { ...record.lastRound, number: 0 } }),
    ).toThrow();
    const isNote = Schema.is(PlaytestNote);
    expect(isNote("")).toBe(true);
    expect(isNote("a\nb")).toBe(false);
    expect(isNote("x".repeat(201))).toBe(false);
    // A NUL fails every spawn; no control character (C0, DEL) is a note.
    for (const note of ["a\u0000b", "tab\there", "del\u007f"]) expect(isNote(note)).toBe(false);
    expect(isNote("Grüße – ok")).toBe(true);
    // A stored note is read back with only its length checked, so an older file still loads.
    expect(decodeRecord({ ...record, note: "old\u0000note" }).note).toBe("old\u0000note");
  });

  it("keeps whether the thread still has to hear about an end T3 made itself", () => {
    // A record from before the worker has none; only due and sent are read.
    expect(decodeRecord(record)).not.toHaveProperty("notice");
    const ended = { ...record, state: "ended", endReason: "idle", endedAt: record.startedAt };
    expect(decodeRecord({ ...ended, notice: "due" }).notice).toBe("due");
    expect(decodeRecord({ ...ended, notice: "sent" }).notice).toBe("sent");
    expect(() => decodeRecord({ ...ended, notice: "maybe" })).toThrow();
  });
});
