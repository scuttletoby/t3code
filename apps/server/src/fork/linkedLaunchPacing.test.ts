/**
 * Fork (placement P1): a delegated task a linked machine sends here passes this machine's usage
 * pacing gate before its slot (`launchGate`, the seam the launch handler calls). Gate rules:
 * `usage/UsagePacingGate.test.ts`; here: who is paced, the typed refusal, its wording, the lowered
 * effort the launch runs at, and that a held task takes no slot.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  EnvironmentId,
  type ModelSelection,
  OrchestratorMcpFailure,
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
  type ServerProviderUsageWindow,
  type ServerSettings as ServerSettingsValue,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import { TestClock } from "effect/testing";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as UsagePacer from "../usage/UsagePacer.ts";
import { launchGate } from "./agentSlotRoutes.ts";

const CODEX = ProviderDriverKind.make("codex");
const MAIN = ProviderInstanceId.make("codex");
const SECOND = ProviderInstanceId.make("codex-second");
const MODEL = "gpt-5.4";
const NOW_MS = Date.parse("2026-11-13T12:00:00.000Z");
const MINUTE = 60 * 1000;
const DAY = 24 * 60 * MINUTE;
const at = (offsetMs: number) => DateTime.formatIso(DateTime.makeUnsafe(NOW_MS + offsetMs));

/** Weekly window half way through: the even pace is 50%. */
const weekly = (usedPercent: number): ServerProviderUsageWindow => ({
  id: "seven_day",
  kind: "weekly",
  label: "Weekly",
  usedPercent,
  resetsAt: at(3.5 * DAY),
  windowDurationMins: 7 * 24 * 60,
});

const provider = (
  instanceId: ProviderInstanceId,
  email: string,
  usedPercent: number,
): ServerProvider => ({
  instanceId,
  driver: CODEX,
  enabled: true,
  installed: true,
  version: "test",
  status: "ready",
  auth: { status: "authenticated", email },
  checkedAt: at(-MINUTE),
  models: [
    {
      slug: MODEL,
      name: MODEL,
      isCustom: false,
      capabilities: {
        optionDescriptors: [
          {
            id: "reasoningEffort",
            label: "Reasoning",
            type: "select",
            options: [
              { id: "low", label: "Low" },
              { id: "medium", label: "Medium" },
              { id: "high", label: "High", isDefault: true },
            ],
          },
        ],
      },
    },
  ],
  slashCommands: [],
  skills: [],
  usageLimits: { checkedAt: at(-MINUTE), windows: [weekly(usedPercent)] },
});

type Caller = "linked agent" | "linked agent, no sender thread" | "linked owner" | "thread here";

interface World {
  readonly settings?: Partial<
    Pick<ServerSettingsValue, "usagePacingGate" | "usagePacingLowerEffort" | "modelPolicy">
  >;
  readonly providers?: ReadonlyArray<ServerProvider>;
  readonly caller?: Caller;
  readonly noPacer?: boolean;
}

const delegatedFrom = {
  environmentId: "environment-there",
  threadId: "thread:parent-there",
  title: "Parent there",
};

/** One launch through the seam; the slot asks it made, and its failure if refused. */
const launch = (
  world: World,
  options: {
    readonly delegated?: boolean;
    readonly replaying?: boolean;
    readonly effort?: string;
  } = {},
) =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW_MS);
    const asks: Array<unknown> = [];
    const caller = world.caller ?? "linked agent";
    const providers = world.providers ?? [
      provider(MAIN, "main@example.com", 60),
      provider(SECOND, "second@example.com", 70),
    ];
    const layers = Layer.mergeAll(
      ServerSettings.layerTest(world.settings ?? { usagePacingGate: "enforce" }),
      Layer.mock(ProviderRegistry.ProviderRegistry)({ getProviders: Effect.succeed(providers) }),
      world.noPacer === true
        ? Layer.empty
        : Layer.mock(UsagePacer.UsagePacer)({
            paceOf: (instanceId) =>
              Effect.succeed(
                UsagePacer.paceInstance({
                  instanceId,
                  provider: providers.find((entry) => entry.instanceId === instanceId),
                  history: [],
                  pacing: UsagePacer.DEFAULT_PACING,
                  nowMs: NOW_MS,
                  staleAfterMs: 30 * MINUTE,
                }),
              ),
          }),
      Layer.mock(SlotGate.SlotGate)({
        acquire: (input) =>
          Effect.sync(() => {
            asks.push(input);
            return { type: "allow", token: asks.length } satisfies SlotGate.SlotAcquire;
          }),
        started: () => Effect.void,
        release: () => Effect.void,
      }),
      Layer.succeed(McpInvocationContext.McpInvocationContext, {
        environmentId: EnvironmentId.make("environment-here"),
        requestNamespace: "link:session",
        thread:
          caller === "thread here"
            ? {
                threadId: ThreadId.make("thread:here"),
                providerSessionId: "s",
                providerInstanceId: MAIN,
              }
            : undefined,
        client:
          caller === "thread here"
            ? undefined
            : {
                sessionId: "link-1",
                label: "Workstation",
                access: "full-access",
                linked: true,
                ...(caller === "linked agent"
                  ? {
                      senderThread: {
                        environmentId: "environment-there",
                        threadId: "thread:parent-there",
                        title: "Parent there",
                      } as never,
                    }
                  : {}),
              },
        issuedAt: 0,
        capabilities: new Set(["orchestration" as const]),
      }),
    );
    const modelSelection: ModelSelection = {
      instanceId: MAIN,
      model: MODEL,
      ...(options.effort === undefined
        ? {}
        : { options: [{ id: "reasoningEffort", value: options.effort }] }),
    };
    const outcome = yield* launchGate(
      caller === "thread here" ? { id: ThreadId.make("thread:here") } : undefined,
      ThreadId.make("thread:child"),
      modelSelection,
      options.replaying === true,
      {
        message: "Do the task.",
        modelSelection,
        ...(options.delegated === false ? {} : { delegatedFrom }),
      },
      [],
    ).pipe(Effect.result, Effect.provide(layers));
    return outcome._tag === "Failure"
      ? { failure: outcome.failure, asks, started: undefined }
      : { failure: undefined, asks, started: outcome.success.modelSelection ?? modelSelection };
  });

const held = (failure: OrchestratorMcpFailure | undefined) => {
  assert.instanceOf(failure, OrchestratorMcpFailure);
  assert.strictEqual(failure!.code, "usage_pacing_hold");
  assert.match(failure!.message, /^Usage pacing on the machine asked to start it held this task: /);
  return failure!.message;
};

describe("a delegated task a linked machine sends (placement P1)", () => {
  it.effect("enforce, ahead: held with the typed code, and it takes no slot", () =>
    Effect.gen(function* () {
      const { failure, asks } = yield* launch({});
      const message = held(failure);
      assert.include(message, "codex is ahead of its usage pace (Weekly 60% used");
      assert.include(message, "No other account of this provider has room.");
      assert.include(message, "ask again then");
      assert.deepStrictEqual(asks, []);
    }),
  );

  it.effect("an account there with room is not offered: it keeps the account it names", () =>
    Effect.gen(function* () {
      const { failure, asks } = yield* launch({
        providers: [
          provider(MAIN, "main@example.com", 60),
          provider(SECOND, "second@example.com", 10),
        ],
      });
      const message = held(failure);
      assert.include(
        message,
        "codex-second has room, but a task from a linked machine keeps the account it names",
      );
      assert.notInclude(message, "target.providerInstanceId");
      assert.deepStrictEqual(asks, []);
    }),
  );

  const effort = (started: ModelSelection | undefined) =>
    started?.options?.find((option) => option.id === "reasoningEffort")?.value;

  it.effect("an effort the owner lowers: the first ask starts here at the lowered level", () =>
    Effect.gen(function* () {
      const settings = {
        usagePacingGate: "enforce",
        usagePacingLowerEffort: { high: "medium" },
      } as const;
      const { failure, asks, started } = yield* launch({ settings }, { effort: "high" });
      assert.isUndefined(failure);
      assert.strictEqual(asks.length, 1);
      assert.strictEqual(effort(started), "medium");
      assert.strictEqual(started?.instanceId, MAIN);
      // No default: the descriptor's default (high) is what is lowered.
      assert.strictEqual(effort((yield* launch({ settings })).started), "medium");
    }),
  );

  it.effect("a first ask at a level the owner lowers to is held, as it is for a task here", () =>
    Effect.gen(function* () {
      const settings = {
        usagePacingGate: "enforce",
        usagePacingLowerEffort: { high: "medium" },
      } as const;
      const { failure, asks } = yield* launch({ settings }, { effort: "medium" });
      assert.notInclude(held(failure), "target.options");
      assert.deepStrictEqual(asks, []);
    }),
  );

  it.effect("chained rows lower one step, once: high runs at medium, not held", () =>
    Effect.gen(function* () {
      const settings = {
        usagePacingGate: "enforce",
        usagePacingLowerEffort: { high: "medium", medium: "low" },
      } as const;
      const high = yield* launch({ settings }, { effort: "high" });
      assert.isUndefined(high.failure);
      assert.strictEqual(effort(high.started), "medium");
      const medium = yield* launch({ settings }, { effort: "medium" });
      assert.isUndefined(medium.failure);
      assert.strictEqual(effort(medium.started), "low");
    }),
  );

  it.effect("a cyclic setting lowers once and starts: nothing to ask again", () =>
    Effect.gen(function* () {
      const settings = {
        usagePacingGate: "enforce",
        usagePacingLowerEffort: { high: "medium", medium: "high" },
      } as const;
      const high = yield* launch({ settings }, { effort: "high" });
      assert.isUndefined(high.failure);
      assert.strictEqual(high.asks.length, 1);
      assert.strictEqual(effort(high.started), "medium");
      const medium = yield* launch({ settings }, { effort: "medium" });
      assert.isUndefined(medium.failure);
      assert.strictEqual(effort(medium.started), "high");
    }),
  );

  // The model policy checks what runs: a lowered effort off the policy's row keeps the checked one,
  // as a task started here does (fork/delegateChecks.ts).
  it.effect("an effort lowered off the model policy's row keeps the checked effort", () =>
    Effect.gen(function* () {
      const row = {
        driver: "codex",
        model: MODEL,
        efforts: ["medium", "high"],
        defaultEffort: "high",
        useFor: "paced work",
      };
      const settings = (efforts: ReadonlyArray<string>) =>
        ({
          usagePacingGate: "enforce",
          usagePacingLowerEffort: { medium: "low" },
          modelPolicy: { mode: "enforce", allow: [{ ...row, efforts }] },
        }) as never;
      const logs: Array<string> = [];
      const kept = yield* launch({ settings: settings(row.efforts) }, { effort: "medium" }).pipe(
        Effect.provide(
          Logger.layer([Logger.make((entry) => logs.push(Logger.formatLogFmt.log(entry)))], {
            mergeWithExisting: false,
          }),
        ),
      );
      assert.isUndefined(kept.failure);
      assert.strictEqual(kept.asks.length, 1);
      assert.strictEqual(effort(kept.started), "medium");
      const warning = logs.filter((line) => line.includes("the task keeps its checked effort"));
      assert.lengthOf(warning, 1);
      assert.include(warning[0], "usagePacingLowerEffort lowered an effort that modelPolicy");
      // With the lower level in the row it runs there.
      const allowed = yield* launch(
        { settings: settings(["low", "medium", "high"]) },
        { effort: "medium" },
      );
      assert.isUndefined(allowed.failure);
      assert.strictEqual(effort(allowed.started), "low");
    }),
  );

  it.effect("advise: the launch runs as asked", () =>
    Effect.gen(function* () {
      const { failure, started } = yield* launch(
        { settings: { usagePacingGate: "advise", usagePacingLowerEffort: { high: "medium" } } },
        { effort: "high" },
      );
      assert.isUndefined(failure);
      assert.strictEqual(effort(started), "high");
    }),
  );

  it.effect("a linked agent whose sender thread is not named is still paced", () =>
    Effect.gen(function* () {
      const { failure, asks } = yield* launch({ caller: "linked agent, no sender thread" });
      held(failure);
      assert.deepStrictEqual(asks, []);
    }),
  );

  it.effect("on pace: it goes on to the slot gate", () =>
    Effect.gen(function* () {
      const { failure, asks } = yield* launch({
        providers: [provider(MAIN, "main@example.com", 20)],
      });
      assert.isUndefined(failure);
      assert.strictEqual(asks.length, 1);
    }),
  );

  it.effect.each([
    { name: "the gate off", world: { settings: { usagePacingGate: "off" } } },
    { name: "advise", world: { settings: { usagePacingGate: "advise" } } },
    { name: "no pacer in this host", world: { noPacer: true } },
    { name: "a launch with no delegatedFrom", world: {}, options: { delegated: false } },
    {
      name: "the owner's client on the linked machine (no delegatedFrom)",
      world: { caller: "linked owner" },
      options: { delegated: false },
    },
    { name: "a thread here (paced by delegate_task here)", world: { caller: "thread here" } },
  ] as ReadonlyArray<{
    readonly name: string;
    readonly world: World;
    readonly options?: { readonly delegated?: boolean };
  }>)("not held: $name", ({ world, options }) =>
    Effect.gen(function* () {
      const { failure, asks } = yield* launch(world, options);
      assert.isUndefined(failure);
      assert.strictEqual(asks.length, 1);
    }),
  );

  it.effect("a replay of an accepted launch is neither paced nor counted again", () =>
    Effect.gen(function* () {
      const { failure, asks } = yield* launch({}, { replaying: true });
      assert.isUndefined(failure);
      assert.deepStrictEqual(asks, []);
    }),
  );
});
