/**
 * Fork (usage pacing part 4): the gate. Paces come from the real `paceInstance`, so the gate is
 * tested against what the pacer says, not against a copy of its rules.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_SERVER_SETTINGS,
  ProviderDriverKind,
  ProviderInstanceId,
  type ProviderOptionDescriptor,
  type ServerProvider,
  type ServerProviderUsageWindow,
  ServerSettings,
  ServerSettingsPatch,
  type UsagePacingInstanceSettings,
} from "@t3tools/contracts";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import {
  DEFAULT_PACING,
  type InstancePace,
  paceInstance,
  type PaceUnknownReason,
} from "./UsagePacer.ts";
import {
  decide,
  effortStep,
  type GateInput,
  type GateOutcome,
  standingOf,
  switchCandidates,
} from "./UsagePacingGate.ts";

const CLAUDE = ProviderDriverKind.make("claudeAgent");
const CODEX = ProviderDriverKind.make("codex");
const MAIN = ProviderInstanceId.make("claude_main");
const SECOND = ProviderInstanceId.make("claude_second");
const THIRD = ProviderInstanceId.make("claude_third");
const NOW_MS = Date.parse("2026-11-13T12:00:00.000Z");
const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const at = (offsetMs: number) => DateTime.formatIso(DateTime.makeUnsafe(NOW_MS + offsetMs));

/** Weekly window; by default half way through, so the even pace is 50%. */
const weekly = (usedPercent: number, resetInMs = 3.5 * DAY): ServerProviderUsageWindow => ({
  id: "seven_day",
  kind: "weekly",
  label: "Weekly",
  usedPercent,
  resetsAt: at(resetInMs),
  windowDurationMins: 7 * 24 * 60,
});

/** Five-hour window half way through: even pace is 50%. */
const session = (usedPercent: number, resetInMs = 2.5 * HOUR): ServerProviderUsageWindow => ({
  id: "five_hour",
  kind: "session",
  label: "Session",
  usedPercent,
  resetsAt: at(resetInMs),
  windowDurationMins: 5 * 60,
});

/** A window without a reset time. */
const open = (usedPercent: number): ServerProviderUsageWindow => ({
  id: "seven_day",
  kind: "weekly",
  label: "Weekly",
  usedPercent,
});

const provider = (
  instanceId: ProviderInstanceId,
  windows: ReadonlyArray<ServerProviderUsageWindow>,
  extra?: {
    readonly driver?: ProviderDriverKind;
    readonly email?: string;
    readonly checkedAtMs?: number;
    readonly unavailable?: "unsupported" | "probeFailed";
  },
): ServerProvider => ({
  instanceId,
  driver: extra?.driver ?? CLAUDE,
  enabled: true,
  installed: true,
  version: "1.0.0",
  status: "ready",
  auth: {
    status: "authenticated",
    ...(extra?.email === undefined ? {} : { email: extra.email }),
  },
  checkedAt: at(extra?.checkedAtMs ?? -MINUTE),
  models: [],
  slashCommands: [],
  skills: [],
  usageLimits: {
    checkedAt: at(extra?.checkedAtMs ?? -MINUTE),
    windows,
    ...(extra?.unavailable === undefined ? {} : { unavailable: { reason: extra.unavailable } }),
  },
});

const paceOfProvider = (
  live: ServerProvider | undefined,
  options?: {
    readonly instanceId?: ProviderInstanceId;
    readonly pacing?: Partial<UsagePacingInstanceSettings>;
    readonly nowMs?: number;
    readonly history?: "unreadable";
  },
): InstancePace =>
  paceInstance({
    instanceId: live?.instanceId ?? options?.instanceId ?? MAIN,
    provider: live,
    history: options?.history ?? [],
    pacing: { ...DEFAULT_PACING, ...options?.pacing },
    nowMs: options?.nowMs ?? NOW_MS,
    // Long, so one reading stays good while a test moves the clock to a window's reset.
    staleAfterMs: 30 * DAY,
  });

const pace = (
  windows: ReadonlyArray<ServerProviderUsageWindow>,
  pacing?: Partial<UsagePacingInstanceSettings>,
) => paceOfProvider(provider(MAIN, windows), pacing === undefined ? {} : { pacing });

/** Every reason the pacer can give. A reason added there fails to compile here until it is listed. */
const UNKNOWN_REASONS: Record<PaceUnknownReason, true> = {
  noInstance: true,
  noLimits: true,
  unreadable: true,
  noReading: true,
  emptyRead: true,
  historyUnreadable: true,
  missing: true,
  stale: true,
  aheadOfClock: true,
  resetPassed: true,
  noResetClock: true,
  ambiguous: true,
};
const REASONS = Object.keys(UNKNOWN_REASONS) as ReadonlyArray<PaceUnknownReason>;

const unknownPace = (reason: PaceUnknownReason, instanceId = MAIN): InstancePace => ({
  instanceId,
  verdict: "unknown",
  unknownReason: reason,
  reservePercent: 0,
  burnFirst: false,
  noLimits: reason === "noLimits",
  checkedAt: null,
  windows: [],
});

describe("UsagePacingGate standingOf", () => {
  it("behind pace: room, by the points left to the target", () => {
    const result = pace([weekly(40)]);
    assert.strictEqual(result.verdict, "behind");
    assert.deepStrictEqual(standingOf(result, NOW_MS), { kind: "room", headroomPercent: 10 });
  });

  it("on pace: room, and the tightest window counts", () => {
    const result = pace([weekly(52), session(20)]);
    assert.strictEqual(result.verdict, "onPace");
    assert.deepStrictEqual(standingOf(result, NOW_MS), { kind: "room", headroomPercent: -2 });
  });

  it("a window without a reset time does not count as room", () => {
    const standing = standingOf(pace([session(20), open(99)]), NOW_MS);
    assert.deepStrictEqual(standing, { kind: "room", headroomPercent: 30 });
  });

  it("over the even pace only: ahead, not hard", () => {
    const result = pace([weekly(60), session(20)]);
    const standing = standingOf(result, NOW_MS);
    assert.strictEqual(standing.kind, "ahead");
    if (standing.kind !== "ahead") return;
    assert.isFalse(standing.hard);
    assert.deepStrictEqual(
      standing.windows.map((window) => window.id),
      ["seven_day"],
    );
  });

  it("in the owner's reserve, or full: ahead and hard", () => {
    for (const result of [
      pace([weekly(85)], { reservePercent: 20 }),
      pace([weekly(85)], { reservePercent: 20, burnFirst: true }),
      pace([weekly(100)]),
    ]) {
      const standing = standingOf(result, NOW_MS);
      assert.strictEqual(standing.kind, "ahead");
      if (standing.kind === "ahead") assert.isTrue(standing.hard);
    }
  });

  it("every unknown reason stays unknown with its reason; only noLimits is its own kind", () => {
    for (const reason of REASONS) {
      assert.deepStrictEqual(
        standingOf(unknownPace(reason), NOW_MS),
        reason === "noLimits" ? { kind: "noLimits" } : { kind: "unknown", reason },
      );
    }
  });

  it("unknown reasons as the pacer really gives them", () => {
    const cases: ReadonlyArray<readonly [PaceUnknownReason, InstancePace]> = [
      ["noInstance", paceOfProvider(undefined)],
      ["noLimits", pace([weekly(99)], { noLimits: true })],
      ["unreadable", paceOfProvider(provider(MAIN, [], { unavailable: "unsupported" }))],
      ["noReading", paceOfProvider(provider(MAIN, [], { unavailable: "probeFailed" }))],
      ["emptyRead", paceOfProvider(provider(MAIN, []))],
      [
        "historyUnreadable",
        paceOfProvider(provider(MAIN, [weekly(40)]), { history: "unreadable" }),
      ],
      ["noResetClock", pace([open(40)])],
      ["resetPassed", pace([weekly(40, -MINUTE)])],
      ["aheadOfClock", paceOfProvider(provider(MAIN, [weekly(40)], { checkedAtMs: HOUR }))],
      ["stale", paceOfProvider(provider(MAIN, [weekly(40)]), { nowMs: NOW_MS + 31 * DAY })],
      ["ambiguous", pace([weekly(40), weekly(41)])],
    ];
    for (const [reason, result] of cases) {
      assert.strictEqual(result.verdict, "unknown", reason);
      assert.strictEqual(result.unknownReason, reason);
      assert.deepStrictEqual(
        standingOf(result, NOW_MS),
        reason === "noLimits" ? { kind: "noLimits" } : { kind: "unknown", reason },
      );
    }
  });
});

describe("UsagePacingGate earliest clear time", () => {
  const notBefore = (result: InstancePace) => {
    const standing = standingOf(result, NOW_MS);
    assert.strictEqual(standing.kind, "ahead");
    return standing.kind === "ahead" ? standing.notBeforeMs : null;
  };

  it("over the even pace: when the pace line plus its band reaches the usage", () => {
    // 60% used half way through a week: the even pace reaches 55% after 8.4 more hours.
    assert.strictEqual(notBefore(pace([weekly(60)])), NOW_MS + 504 * MINUTE + MINUTE);
  });

  it("in the reserve: when enough of the reserve is released", () => {
    // Reserve 20 is released over the last 12 hours: 10 points are free 6 hours before the reset.
    assert.strictEqual(
      notBefore(pace([weekly(90)], { reservePercent: 20 })),
      NOW_MS + 3.5 * DAY - 6 * HOUR + MINUTE,
    );
    assert.strictEqual(
      notBefore(pace([weekly(85)], { reservePercent: 20, burnFirst: true })),
      NOW_MS + 3.5 * DAY - 9 * HOUR + MINUTE,
    );
  });

  it("a full window clears at its reset", () => {
    assert.strictEqual(notBefore(pace([weekly(100)])), NOW_MS + 3.5 * DAY);
    assert.strictEqual(
      notBefore(pace([weekly(100)], { reservePercent: 20, burnFirst: true })),
      NOW_MS + 3.5 * DAY,
    );
  });

  it("two windows ahead: the later of the two", () => {
    assert.strictEqual(notBefore(pace([session(70)])), NOW_MS + 45 * MINUTE + MINUTE);
    assert.strictEqual(notBefore(pace([session(70), weekly(60)])), NOW_MS + 504 * MINUTE + MINUTE);
  });

  it("no reset time: not known", () => {
    const result = pace([open(100)], { burnFirst: true });
    assert.strictEqual(result.verdict, "ahead");
    assert.isNull(notBefore(result));
  });

  it("matches the pacer: still ahead two minutes before, not ahead at the time", () => {
    let checked = 0;
    for (const burnFirst of [false, true]) {
      for (const reservePercent of [0, 10, 30]) {
        for (const resetInMs of [HOUR, 5 * HOUR, 13 * HOUR, 2 * DAY, 3.5 * DAY, 6 * DAY]) {
          for (let used = 3; used <= 100; used += 7) {
            const live = provider(MAIN, [weekly(used, resetInMs)]);
            const pacing = { reservePercent, burnFirst };
            const now = paceOfProvider(live, { pacing });
            if (now.verdict !== "ahead") continue;
            const clearsAt = notBefore(now);
            const label = `used ${used}, reset in ${resetInMs / HOUR}h, reserve ${reservePercent}, burnFirst ${burnFirst}`;
            assert.isNotNull(clearsAt, label);
            assert.isAtMost(clearsAt!, NOW_MS + resetInMs, label);
            assert.isAbove(clearsAt!, NOW_MS, label);
            assert.notStrictEqual(
              paceOfProvider(live, { pacing, nowMs: clearsAt! }).verdict,
              "ahead",
              `${label}: at the time`,
            );
            if (clearsAt! - 2 * MINUTE > NOW_MS) {
              assert.strictEqual(
                paceOfProvider(live, { pacing, nowMs: clearsAt! - 2 * MINUTE }).verdict,
                "ahead",
                `${label}: two minutes before`,
              );
            }
            checked += 1;
          }
        }
      }
    }
    assert.isAbove(checked, 80);
  });
});

describe("UsagePacingGate switchCandidates", () => {
  const current = provider(MAIN, [weekly(60)], { email: "main@example.com" });
  const candidatesOf = (
    others: ReadonlyArray<ServerProvider>,
    paces?: ReadonlyArray<InstancePace>,
  ) =>
    switchCandidates({
      current,
      providers: [current, ...others],
      paces: paces ?? others.map((other) => paceOfProvider(other)),
      nowMs: NOW_MS,
    });

  it("most room first", () => {
    const some = provider(SECOND, [weekly(45)], { email: "second@example.com" });
    const most = provider(THIRD, [weekly(10)], { email: "third@example.com" });
    assert.deepStrictEqual(candidatesOf([some, most]), [
      { instanceId: THIRD, headroomPercent: 40 },
      { instanceId: SECOND, headroomPercent: 5 },
    ]);
  });

  it("never the same account, another provider, or the instance itself", () => {
    const sameLogin = provider(SECOND, [weekly(10)], { email: "Main@Example.com" });
    const otherDriver = provider(THIRD, [weekly(10)], { driver: CODEX, email: "x@example.com" });
    assert.deepStrictEqual(candidatesOf([sameLogin, otherDriver, current]), []);
  });

  it("an account whose login is not known is its own account", () => {
    const noLogin = provider(SECOND, [weekly(10)]);
    assert.deepStrictEqual(candidatesOf([noLogin]), [{ instanceId: SECOND, headroomPercent: 40 }]);
  });

  it("never one that is ahead, unknown, marked noLimits, or without a pace", () => {
    const second = provider(SECOND, [weekly(10)], { email: "second@example.com" });
    assert.deepStrictEqual(candidatesOf([second], []), []);
    assert.deepStrictEqual(
      candidatesOf([second], [paceOfProvider(second, { pacing: { noLimits: true } })]),
      [],
    );
    for (const reason of REASONS) {
      assert.deepStrictEqual(candidatesOf([second], [unknownPace(reason, SECOND)]), [], reason);
    }
    const ahead = provider(SECOND, [weekly(70)], { email: "second@example.com" });
    assert.deepStrictEqual(candidatesOf([ahead]), []);
  });
});

const effort = (
  extra?: Partial<Extract<ProviderOptionDescriptor, { type: "select" }>>,
): ProviderOptionDescriptor => ({
  id: "effort",
  label: "Effort",
  type: "select",
  options: [
    { id: "low", label: "Low" },
    { id: "medium", label: "Medium" },
    { id: "high", label: "High", isDefault: true },
  ],
  ...extra,
});
const fastMode: ProviderOptionDescriptor = { id: "fastMode", label: "Fast", type: "boolean" };

describe("UsagePacingGate effortStep", () => {
  const stepOf = (input: Partial<Parameters<typeof effortStep>[0]>) =>
    effortStep({
      descriptors: [fastMode, effort()],
      options: undefined,
      lowerEffort: {},
      ...input,
    });

  it("nothing listed: no step (the default)", () => {
    assert.isUndefined(stepOf({ options: [{ id: "effort", value: "high" }] }));
  });

  it("the task's own level decides", () => {
    assert.deepStrictEqual(
      stepOf({ options: [{ id: "effort", value: "high" }], lowerEffort: { high: "medium" } }),
      { optionId: "effort", from: "high", to: "medium" },
    );
    assert.isUndefined(
      stepOf({ options: [{ id: "effort", value: "low" }], lowerEffort: { high: "medium" } }),
    );
  });

  it("no level given: the model's current one, else its default", () => {
    assert.deepStrictEqual(stepOf({ lowerEffort: { high: "low" } }), {
      optionId: "effort",
      from: "high",
      to: "low",
    });
    assert.deepStrictEqual(
      stepOf({
        descriptors: [effort({ currentValue: "medium" })],
        lowerEffort: { medium: "low", high: "low" },
      }),
      { optionId: "effort", from: "medium", to: "low" },
    );
    const noDefault = effort({ options: [{ id: "low", label: "Low" }] });
    assert.isUndefined(stepOf({ descriptors: [noDefault], lowerEffort: { high: "low" } }));
  });

  it("a level listed to itself is a step to itself", () => {
    assert.deepStrictEqual(stepOf({ lowerEffort: { high: "high" } }), {
      optionId: "effort",
      from: "high",
      to: "high",
    });
  });

  it("a level the model does not offer: no step", () => {
    assert.isUndefined(stepOf({ lowerEffort: { high: "minimal" } }));
  });

  it("finds the effort control by its role or its usual ids only", () => {
    const lowerEffort = { high: "low" };
    const thought = effort({ id: "thought_level", role: "reasoning" });
    assert.strictEqual(stepOf({ descriptors: [thought], lowerEffort })?.optionId, "thought_level");
    const reasoning = effort({ id: "reasoningEffort" });
    assert.strictEqual(
      stepOf({ descriptors: [reasoning], lowerEffort })?.optionId,
      "reasoningEffort",
    );
    assert.isUndefined(stepOf({ descriptors: [effort({ id: "verbosity" })], lowerEffort }));
    assert.isUndefined(stepOf({ descriptors: [fastMode], lowerEffort }));
    assert.isUndefined(stepOf({ descriptors: undefined, lowerEffort }));
  });

  it("a key such as constructor is not a listed level", () => {
    const odd = effort({ options: [{ id: "constructor", label: "Odd", isDefault: true }] });
    assert.isUndefined(stepOf({ descriptors: [odd], lowerEffort: {} }));
  });
});

interface Target {
  readonly on: ProviderInstanceId;
}

describe("UsagePacingGate decide", () => {
  const main = (windows: ReadonlyArray<ServerProviderUsageWindow>) =>
    provider(MAIN, windows, { email: "main@example.com" });
  const second = (windows: ReadonlyArray<ServerProviderUsageWindow>) =>
    provider(SECOND, windows, { email: "second@example.com" });
  const third = (windows: ReadonlyArray<ServerProviderUsageWindow>) =>
    provider(THIRD, windows, { email: "third@example.com" });

  const run = (
    providers: ReadonlyArray<ServerProvider>,
    extra?: Partial<GateInput<Target, never>> & {
      readonly paces?: Readonly<Record<string, InstancePace | undefined>>;
      readonly pacing?: Partial<UsagePacingInstanceSettings>;
      readonly cannotRunOn?: ReadonlyArray<ProviderInstanceId>;
    },
  ) => {
    const calls = {
      paceOf: [] as Array<ProviderInstanceId>,
      targetOn: [] as Array<ProviderInstanceId>,
    };
    const { paces, pacing, cannotRunOn, ...input } = extra ?? {};
    return decide<Target, never>({
      mode: "enforce",
      lowerEffort: {},
      nowMs: NOW_MS,
      instanceId: MAIN,
      pinned: false,
      providers,
      descriptors: [effort()],
      options: undefined,
      paceOf: (instanceId) =>
        Effect.sync(() => {
          calls.paceOf.push(instanceId);
          if (paces !== undefined && Object.hasOwn(paces, instanceId)) return paces[instanceId];
          const live = providers.find((entry) => entry.instanceId === instanceId);
          return paceOfProvider(live, {
            instanceId,
            ...(instanceId === MAIN && pacing !== undefined ? { pacing } : {}),
          });
        }),
      targetOn: (instanceId) =>
        Effect.sync(() => {
          calls.targetOn.push(instanceId);
          return cannotRunOn?.includes(instanceId) === true ? undefined : { on: instanceId };
        }),
      ...input,
    }).pipe(Effect.map((outcome) => ({ outcome, calls })));
  };

  const noteOf = (outcome: GateOutcome<Target>) =>
    outcome.action === "hold" ? outcome.message : (outcome.note ?? "");

  const MODES = ["advise", "enforce"] as const;

  it.effect("off: passes without reading a pace or a target", () =>
    Effect.gen(function* () {
      const { outcome, calls } = yield* run([main([weekly(100)]), second([weekly(10)])], {
        mode: "off",
      });
      assert.deepStrictEqual(outcome, { action: "pass" });
      assert.deepStrictEqual(calls, { paceOf: [], targetOn: [] });
    }),
  );

  it.effect.each(MODES)("%s: room passes without a note, and no other account is read", (mode) =>
    Effect.gen(function* () {
      for (const windows of [[weekly(40)], [weekly(52)]]) {
        const { outcome, calls } = yield* run([main(windows), second([weekly(10)])], { mode });
        assert.deepStrictEqual(outcome, { action: "pass" });
        assert.deepStrictEqual(calls, { paceOf: [MAIN], targetOn: [] });
      }
    }),
  );

  it.effect.each(MODES)(
    "%s: noLimits passes on purpose, without a note, whatever the usage",
    (mode) =>
      Effect.gen(function* () {
        const { outcome, calls } = yield* run([main([weekly(100)]), second([weekly(10)])], {
          mode,
          pacing: { noLimits: true },
        });
        assert.deepStrictEqual(outcome, { action: "pass" });
        assert.deepStrictEqual(calls, { paceOf: [MAIN], targetOn: [] });
      }),
  );

  it.effect.each(MODES)(
    "%s: every other unknown reason passes with a note that names it, never a stop",
    (mode) =>
      Effect.gen(function* () {
        for (const reason of REASONS.filter((entry) => entry !== "noLimits")) {
          const { outcome, calls } = yield* run([main([weekly(100)]), second([weekly(10)])], {
            mode,
            paces: { [MAIN]: unknownPace(reason) },
          });
          assert.strictEqual(outcome.action, "pass", reason);
          assert.include(noteOf(outcome), `is not known (${reason})`);
          assert.include(noteOf(outcome), "was not paced");
          assert.deepStrictEqual(calls, { paceOf: [MAIN], targetOn: [] });
        }
      }),
  );

  it.effect.each(MODES)("%s: a pace that cannot be read passes with a note", (mode) =>
    Effect.gen(function* () {
      const { outcome } = yield* run([main([weekly(100)])], {
        mode,
        paces: { [MAIN]: undefined },
      });
      assert.strictEqual(outcome.action, "pass");
      assert.include(noteOf(outcome), "could not be read");
    }),
  );

  it.effect.each(MODES)("%s: an instance that is not configured passes with a note", (mode) =>
    Effect.gen(function* () {
      const { outcome } = yield* run([second([weekly(10)])], { mode });
      assert.strictEqual(outcome.action, "pass");
      assert.include(noteOf(outcome), "is not known (noInstance)");
    }),
  );

  it.effect("enforce, ahead: runs on the account with the most room", () =>
    Effect.gen(function* () {
      const { outcome, calls } = yield* run([
        main([weekly(60)]),
        second([weekly(45)]),
        third([weekly(10)]),
      ]);
      assert.strictEqual(outcome.action, "switch");
      if (outcome.action !== "switch") return;
      assert.deepStrictEqual(outcome.target, { on: THIRD });
      assert.include(
        outcome.note,
        "claude_main is ahead of its usage pace (Weekly 60% used, target 50%)",
      );
      assert.include(outcome.note, "runs on claude_third");
      assert.deepStrictEqual(calls.targetOn, [THIRD]);
    }),
  );

  it.effect("enforce, ahead and hard: still moves to an account with room", () =>
    Effect.gen(function* () {
      const { outcome } = yield* run([main([weekly(100)]), second([weekly(45)])]);
      assert.strictEqual(outcome.action, "switch");
    }),
  );

  it.effect("enforce, ahead: an account the task cannot run on is passed over", () =>
    Effect.gen(function* () {
      const providers = [main([weekly(60)]), second([weekly(45)]), third([weekly(10)])];
      const next = yield* run(providers, { cannotRunOn: [THIRD] });
      assert.deepStrictEqual(next.outcome.action === "switch" ? next.outcome.target : undefined, {
        on: SECOND,
      });
      assert.deepStrictEqual(next.calls.targetOn, [THIRD, SECOND]);
      const none = yield* run(providers, { cannotRunOn: [THIRD, SECOND] });
      assert.strictEqual(none.outcome.action, "hold");
      assert.include(noteOf(none.outcome), "No other account of this provider has room.");
    }),
  );

  it.effect(
    "enforce, ahead: never moves to an account that is ahead, unknown or another provider",
    () =>
      Effect.gen(function* () {
        const { outcome, calls } = yield* run([
          main([weekly(60)]),
          second([weekly(70)]),
          third([open(1)]),
          provider(ProviderInstanceId.make("codex"), [weekly(1)], { driver: CODEX }),
        ]);
        assert.strictEqual(outcome.action, "hold");
        assert.deepStrictEqual(calls.targetOn, []);
        assert.deepStrictEqual(calls.paceOf, [MAIN, SECOND, THIRD]);
      }),
  );

  it.effect(
    "enforce, ahead, instance named by the caller: held, naming the account with room",
    () =>
      Effect.gen(function* () {
        const { outcome } = yield* run([main([weekly(60)]), second([weekly(45)])], {
          pinned: true,
          lowerEffort: { high: "low" },
        });
        assert.strictEqual(outcome.action, "hold");
        assert.include(
          noteOf(outcome),
          "claude_second has room: pass it as target.providerInstanceId",
        );
      }),
  );

  it.effect("enforce, over the even pace, no room elsewhere: the effort the owner listed", () =>
    Effect.gen(function* () {
      const { outcome } = yield* run([main([weekly(60)])], { lowerEffort: { high: "medium" } });
      assert.strictEqual(outcome.action, "lowerEffort");
      if (outcome.action !== "lowerEffort") return;
      assert.deepStrictEqual(outcome.step, { optionId: "effort", from: "high", to: "medium" });
      assert.include(outcome.note, "effort medium instead of high");
    }),
  );

  it.effect(
    "enforce, over the even pace: a level listed to itself runs as asked, with a note",
    () =>
      Effect.gen(function* () {
        const { outcome } = yield* run([main([weekly(60)])], {
          options: [{ id: "effort", value: "low" }],
          lowerEffort: { high: "medium", low: "low" },
        });
        assert.strictEqual(outcome.action, "pass");
        assert.include(noteOf(outcome), "lets effort low run");
      }),
  );

  it.effect("enforce, in the reserve or full: effort is never the way out", () =>
    Effect.gen(function* () {
      for (const [windows, pacing] of [
        [[weekly(100)], {}],
        [[weekly(85)], { reservePercent: 20 }],
      ] as const) {
        const { outcome } = yield* run([main(windows)], { lowerEffort: { high: "low" }, pacing });
        assert.strictEqual(outcome.action, "hold");
      }
    }),
  );

  it.effect("enforce, ahead, nothing else: held, with the earliest time it can clear", () =>
    Effect.gen(function* () {
      const { outcome } = yield* run([main([weekly(60)])]);
      assert.deepStrictEqual(outcome, {
        action: "hold",
        message:
          "Usage pacing held this task: claude_main is ahead of its usage pace (Weekly 60% used, target 50%). " +
          "No other account of this provider has room. " +
          `With nothing more spent it clears at ${at(505 * MINUTE)} at the earliest; ask again then.`,
        notBefore: at(505 * MINUTE),
      });
    }),
  );

  it.effect("enforce, ahead, no reset time: held without a time", () =>
    Effect.gen(function* () {
      const { outcome } = yield* run([main([open(100)])], { pacing: { burnFirst: true } });
      assert.strictEqual(outcome.action, "hold");
      if (outcome.action !== "hold") return;
      assert.isNull(outcome.notBefore);
      assert.include(outcome.message, "Its reset time is not known; ask again later.");
    }),
  );

  it.effect("advise: says what enforce would do and always passes", () =>
    Effect.gen(function* () {
      const cases: ReadonlyArray<readonly [ReturnType<typeof run>, string]> = [
        [
          run([main([weekly(60)]), second([weekly(45)])], { mode: "advise" }),
          "would run this task there",
        ],
        [
          run([main([weekly(60)]), second([weekly(45)])], { mode: "advise", pinned: true }),
          '"enforce" would hold this task',
        ],
        [
          run([main([weekly(60)])], { mode: "advise", lowerEffort: { high: "medium" } }),
          "would run this task at effort medium instead of high",
        ],
        [run([main([weekly(100)])], { mode: "advise" }), '"enforce" would hold this task'],
      ];
      for (const [decided, text] of cases) {
        const { outcome } = yield* decided;
        assert.strictEqual(outcome.action, "pass", text);
        assert.include(noteOf(outcome), text);
        assert.include(noteOf(outcome), "Usage pacing (advice only, the task runs as asked)");
      }
    }),
  );
});

describe("UsagePacingGate settings", () => {
  const decode = Schema.decodeUnknownSync(ServerSettings);
  const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

  it("a settings file from before the gate: off, and no effort listed", () => {
    const settings = decode({});
    assert.strictEqual(settings.usagePacingGate, "off");
    assert.deepStrictEqual(settings.usagePacingLowerEffort, {});
    assert.strictEqual(DEFAULT_SERVER_SETTINGS.usagePacingGate, "off");
  });

  it("a mode that is not one of the three is refused, not read as on", () => {
    assert.throws(() => decode({ usagePacingGate: "on" }));
    assert.throws(() => decodePatch({ usagePacingGate: "strict" }));
  });

  it("a patch of the mode keeps the effort list; a patch of the list replaces it whole", () => {
    const stored = applyServerSettingsPatch(
      DEFAULT_SERVER_SETTINGS,
      decodePatch({
        usagePacingGate: "enforce",
        usagePacingLowerEffort: { xhigh: "high", high: "medium" },
      }),
    );
    assert.strictEqual(stored.usagePacingGate, "enforce");
    const advised = applyServerSettingsPatch(stored, decodePatch({ usagePacingGate: "advise" }));
    assert.deepStrictEqual(advised.usagePacingLowerEffort, { xhigh: "high", high: "medium" });
    const relisted = applyServerSettingsPatch(
      advised,
      decodePatch({ usagePacingLowerEffort: { high: "low" } }),
    );
    assert.deepStrictEqual(relisted.usagePacingLowerEffort, { high: "low" });
    assert.strictEqual(relisted.usagePacingGate, "advise");
    const cleared = applyServerSettingsPatch(relisted, decodePatch({ usagePacingLowerEffort: {} }));
    assert.deepStrictEqual(cleared.usagePacingLowerEffort, {});
  });
});
