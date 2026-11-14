import { it as effectIt } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Logger from "effect/Logger";
import * as Schema from "effect/Schema";
import { describe, expect, it } from "vite-plus/test";

import {
  DEFAULT_SERVER_SETTINGS,
  PROJECT_SCOPED_SERVER_SETTING_KEYS,
  ProjectSettingsOverrides,
  ServerSettings,
  ServerSettingsPatch,
} from "./settings.ts";
import {
  AgentSlotsSettings,
  WaitCheck,
  WaitChecks,
  WaitForInput,
  WaitForSettings,
  WaitRecord,
  WaitStoreFile,
} from "./waitFor.ts";

const decodeServerSettings = Schema.decodeUnknownSync(ServerSettings);
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeCheck = Schema.decodeUnknownSync(WaitCheck);
const decodeChecks = Schema.decodeUnknownSync(WaitChecks);
const decodeStore = Schema.decodeUnknownSync(WaitStoreFile);
const decodeRecord = Schema.decodeUnknownSync(WaitRecord);
const decodeInput = Schema.decodeUnknownSync(WaitForInput);
const decodeWaitFor = Schema.decodeSync(WaitForSettings);
const decodeAgentSlots = Schema.decodeSync(AgentSlotsSettings);

const record = {
  id: "w_abcdefgh23",
  threadId: "thread-a",
  projectId: "project-a",
  key: "aaaaaaaaaaaaaaaa",
  createdAt: "2026-11-14T10:00:00.000Z",
  deadlineAt: "2026-11-15T10:00:00.000Z",
  state: "waiting",
  attempt: 0,
  kind: "pr",
  spec: { number: 12, until: "checks" },
};

describe("wait settings", () => {
  it("decodes an old settings file without the keys to defaults that are off", () => {
    const settings = decodeServerSettings({});
    expect(settings.waitFor).toEqual(decodeWaitFor({}));
    expect(settings.waitFor.enabled).toBe(false);
    expect(settings.waitChecks).toEqual({});
    expect(settings.agentSlots).toEqual(decodeAgentSlots({}));
    expect(settings.agentSlots.enabled).toBe(false);
    expect(DEFAULT_SERVER_SETTINGS.waitFor.enabled).toBe(false);
    expect(DEFAULT_SERVER_SETTINGS.agentSlots.enabled).toBe(false);
  });

  it("fills missing fields of a partly written block", () => {
    const settings = decodeServerSettings({ waitFor: { enabled: true, prSweepSeconds: 60 } });
    expect(settings.waitFor.enabled).toBe(true);
    expect(settings.waitFor.prSweepSeconds).toBe(60);
    expect(settings.waitFor.defaultTimeoutHours).toBe(24);
    expect(settings.waitFor.stoppedHandOverHours).toBe(24);
  });

  it("accepts partial patches", () => {
    expect(decodePatch({ waitFor: { enabled: true } })).toEqual({ waitFor: { enabled: true } });
    expect(decodePatch({ agentSlots: { maxPerAccount: { claude: 2 } } })).toEqual({
      agentSlots: { maxPerAccount: { claude: 2 } },
    });
  });

  it("lets a project override the checks, and only the checks", () => {
    expect(PROJECT_SCOPED_SERVER_SETTING_KEYS).toContain("waitChecks");
    expect(PROJECT_SCOPED_SERVER_SETTING_KEYS).not.toContain("waitFor");
    expect(PROJECT_SCOPED_SERVER_SETTING_KEYS).not.toContain("agentSlots");
  });
});

describe("WaitCheck", () => {
  it("fills defaults and keeps argv", () => {
    expect(decodeCheck({ argv: ["/usr/bin/true"] })).toEqual({
      argv: ["/usr/bin/true"],
      maxArgs: 0,
      argPattern: "^[A-Za-z0-9._:/@=+-]{1,200}$",
      intervalSeconds: 30,
      timeoutSeconds: 20,
      description: "",
    });
  });

  it("refuses an empty argv, a pattern that does not compile and out-of-range numbers", () => {
    expect(() => decodeCheck({ argv: [] })).toThrow();
    expect(() => decodeCheck({ argv: ["x"], argPattern: "(" })).toThrow();
    expect(() => decodeCheck({ argv: ["x"], maxArgs: 21 })).toThrow();
    expect(() => decodeCheck({ argv: ["x"], intervalSeconds: 4 })).toThrow();
    expect(() => decodeCheck({ argv: ["x"], timeoutSeconds: 601 })).toThrow();
  });

  it("refuses check names that are not lower-case words", () => {
    expect(Object.keys(decodeChecks({ "lease-free": { argv: ["x"] } }))).toEqual(["lease-free"]);
    expect(() => decodeChecks({ "Lease Free": { argv: ["x"] } })).toThrow();
    expect(() => decodeChecks({ "-lease": { argv: ["x"] } })).toThrow();
  });
});

describe("bad checks in the settings file (read drops, save refuses)", () => {
  const good = { argv: ["/usr/bin/true"] };
  const bad = [
    ["a bad name", "Lease_Free", good],
    ["an interval below 5", "lease-short", { argv: ["/bin/true"], intervalSeconds: 3 }],
    ["a pattern that does not compile", "lease-regex", { argv: ["/bin/true"], argPattern: "([" }],
  ] as const;

  /** Decodes like the server's read and returns the exit and the warnings it logged. */
  const decodeLogged = Effect.fn("decodeLogged")(function* <A>(
    schema: Schema.Codec<A, unknown>,
    input: unknown,
  ) {
    const warnings: Array<unknown> = [];
    const logger = Logger.make(({ logLevel, message }) => {
      if (logLevel === "Warn") warnings.push(message);
    });
    const exit = yield* Schema.decodeUnknownEffect(schema)(input).pipe(
      Effect.provide(Logger.layer([logger], { mergeWithExisting: false })),
      Effect.exit,
    );
    return { exit, warnings };
  });

  for (const [what, name, check] of bad) {
    effectIt.effect(`drops ${what} with a warning; the other settings and checks load`, () =>
      Effect.gen(function* () {
        const { exit, warnings } = yield* decodeLogged(ServerSettings, {
          enableProviderUpdateChecks: false,
          waitChecks: { "lease-free": good, [name]: check },
        });
        expect(Exit.isSuccess(exit)).toBe(true);
        const settings = Exit.isSuccess(exit) ? exit.value : undefined;
        expect(settings?.enableProviderUpdateChecks).toBe(false);
        expect(Object.keys(settings?.waitChecks ?? {})).toEqual(["lease-free"]);
        expect(JSON.stringify(warnings)).toContain(name);
      }),
    );
  }

  effectIt.effect("drops a bad check of a project override the same way", () =>
    Effect.gen(function* () {
      const { exit, warnings } = yield* decodeLogged(ProjectSettingsOverrides, {
        waitChecks: { "lease-free": good, Lease_Free: good },
      });
      expect(Exit.isSuccess(exit) ? Object.keys(exit.value.waitChecks ?? {}) : null).toEqual([
        "lease-free",
      ]);
      expect(warnings).toHaveLength(1);
    }),
  );

  effectIt.effect("logs nothing when every check is good", () =>
    Effect.gen(function* () {
      const { warnings } = yield* decodeLogged(ServerSettings, {
        waitChecks: { "lease-free": good },
      });
      expect(warnings).toEqual([]);
    }),
  );

  it.each(bad)("refuses %s in a settings patch", (_, name, check) => {
    expect(() => decodePatch({ waitChecks: { [name]: check } })).toThrow();
    expect(() =>
      decodePatch({ projectSettingsOverrides: { "project-a": { waitChecks: { [name]: check } } } }),
    ).toThrow();
  });
});

describe("WaitStoreFile", () => {
  it("decodes an empty store", () => {
    expect(decodeStore({ version: 1, waits: [] })).toEqual({ version: 1, waits: [] });
  });

  it("refuses a store written by a newer version", () => {
    expect(() => decodeStore({ version: 2, waits: [] })).toThrow();
  });

  it("refuses a record whose spec does not fit its kind", () => {
    expect(decodeRecord(record).kind).toBe("pr");
    expect(() => decodeRecord({ ...record, kind: "file" })).toThrow();
    expect(() => decodeRecord({ ...record, id: "w_UPPERCASE1" })).toThrow();
  });

  it("keeps the hand-over fields", () => {
    const handedOver = decodeRecord({
      ...record,
      state: "delivering",
      via: ["thread-child"],
      handedOverBecause: "gone",
      stoppedSince: "2026-11-14T11:00:00.000Z",
    });
    expect(handedOver.via).toEqual(["thread-child"]);
    expect(handedOver.handedOverBecause).toBe("gone");
  });
});

describe("WaitForInput", () => {
  it("is one flat object so provider tool schemas accept it", () => {
    expect(decodeInput({ kind: "check", check: { name: "lease-free", args: ["a"] } })).toEqual({
      kind: "check",
      check: { name: "lease-free", args: ["a"] },
    });
    expect(() => decodeInput({ kind: "nope" })).toThrow();
    expect(() =>
      decodeInput({
        kind: "check",
        check: { name: "c", args: Array.from({ length: 21 }, () => "a") },
      }),
    ).toThrow();
  });
});
