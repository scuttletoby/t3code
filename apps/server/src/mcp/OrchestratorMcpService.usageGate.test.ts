/**
 * Fork (usage pacing part 4): the gate as `delegate_task` applies it. The rules themselves are in
 * `usage/UsagePacingGate.test.ts`; here: what reaches the dispatched command, what the caller gets
 * back, and that nothing is read while the gate is off.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  EnvironmentId,
  NodeId,
  type OrchestratorMcpTarget,
  type OrchestrationV2ThreadProjection,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  RunId,
  type RuntimeMode,
  type ServerProvider,
  type ServerProviderUsageWindow,
  type ServerSettings as ServerSettingsValue,
  ServerSettingsError,
  ThreadId,
} from "@t3tools/contracts";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { TestClock } from "effect/testing";

import * as ProviderAdapterRegistry from "../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ThreadLaunchService from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ScheduledTaskService from "../scheduledTasks/ScheduledTaskService.ts";
import * as SecretRequests from "../secrets/SecretRequests.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as UsagePacer from "../usage/UsagePacer.ts";
import * as GitVcsDriver from "../vcs/GitVcsDriver.ts";
import type { McpInvocationScope } from "./McpInvocationContext.ts";
import * as OrchestratorMcpService from "./OrchestratorMcpService.ts";

const CODEX = ProviderDriverKind.make("codex");
const MAIN = ProviderInstanceId.make("codex");
const SECOND = ProviderInstanceId.make("codex-second");
const MODEL = "gpt-5.4";
const NOW_MS = Date.parse("2026-11-13T12:00:00.000Z");
const MINUTE = 60 * 1000;
const DAY = 24 * 60 * MINUTE;

const parentThreadId = ThreadId.make("thread:usage-gate-parent");
const childThreadId = ThreadId.make("thread:usage-gate-child");
const parentRunId = RunId.make("run:usage-gate-parent");
const taskId = NodeId.make("node:usage-gate-task");
const projectId = ProjectId.make("project:usage-gate");

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
  windows: ReadonlyArray<ServerProviderUsageWindow>,
  supportedRuntimeModes?: ReadonlyArray<RuntimeMode>,
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
  usageLimits: { checkedAt: at(-MINUTE), windows },
  ...(supportedRuntimeModes === undefined ? {} : { supportedRuntimeModes }),
});

const main = (usedPercent: number) => provider(MAIN, "main@example.com", [weekly(usedPercent)]);
const second = (usedPercent: number, modes?: ReadonlyArray<RuntimeMode>) =>
  provider(SECOND, "second@example.com", [weekly(usedPercent)], modes);

const scope: McpInvocationScope = {
  environmentId: EnvironmentId.make("environment:usage-gate"),
  requestNamespace: "provider-session:usage-gate",
  thread: {
    threadId: parentThreadId,
    providerSessionId: "provider-session:usage-gate",
    providerInstanceId: MAIN,
  },
  client: undefined,
  capabilities: new Set(["orchestration"]),
  issuedAt: 1,
};

const parentProjection = (subagents: ReadonlyArray<unknown>): OrchestrationV2ThreadProjection =>
  ({
    thread: {
      id: parentThreadId,
      projectId,
      title: "Parent",
      createdBy: "user",
      creationSource: "web",
      modelSelection: { instanceId: MAIN, model: MODEL },
      runtimeMode: "full-access",
      interactionMode: "default",
    },
    runs: [
      {
        id: parentRunId,
        ordinal: 1,
        status: "running",
        rootNodeId: NodeId.make("node:usage-gate-root"),
        providerInstanceId: MAIN,
        modelSelection: { instanceId: MAIN, model: MODEL },
      },
    ],
    contextTransfers: [],
    subagents,
  }) as unknown as OrchestrationV2ThreadProjection;

const childProjection = {
  thread: { id: childThreadId, title: "Child" },
  runs: [],
  contextTransfers: [],
  messages: [],
  subagents: [],
  providerThreads: [],
  providerTurns: [],
  runtimeRequests: [],
  turnItems: [],
} as unknown as OrchestrationV2ThreadProjection;

interface Dispatched {
  readonly modelSelection: {
    readonly instanceId: string;
    readonly model: string;
    readonly options?: ReadonlyArray<{ readonly id: string; readonly value: unknown }>;
  };
}

/**
 * One `delegate_task` call. `pacer`: "real" paces the given providers as the pacer would,
 * "untouched" fails the test if a pace is read, "absent" is a server without the pacer.
 */
const delegate = (input: {
  readonly settings?: Partial<
    Pick<ServerSettingsValue, "usagePacingGate" | "usagePacingLowerEffort">
  >;
  readonly providers: ReadonlyArray<ServerProvider>;
  readonly pacer?: "real" | "untouched" | "absent" | "failing";
  readonly target?: OrchestratorMcpTarget;
  readonly disabledForProject?: ReadonlyArray<ProviderInstanceId>;
  readonly retryOfAccepted?: boolean;
}) =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW_MS);
    const dispatched: Array<Dispatched> = [];
    const task = {
      id: taskId,
      threadId: parentThreadId,
      origin: "app_owned",
      childThreadId,
      providerInstanceId: MAIN,
      model: MODEL,
      status: "running",
      result: null,
    };
    const enabled = (instanceId: ProviderInstanceId) =>
      input.disabledForProject?.includes(instanceId) !== true;
    const pacer = input.pacer ?? "real";
    const layerPacer =
      pacer === "absent"
        ? Layer.empty
        : Layer.mock(UsagePacer.UsagePacer)(
            pacer === "untouched"
              ? {}
              : {
                  paceOf: (instanceId) =>
                    pacer === "failing"
                      ? Effect.fail(
                          new ServerSettingsError({
                            settingsPath: "settings.json",
                            operation: "read-file",
                          }),
                        )
                      : Effect.succeed(
                          UsagePacer.paceInstance({
                            instanceId,
                            provider: input.providers.find(
                              (entry) => entry.instanceId === instanceId,
                            ),
                            history: [],
                            pacing: UsagePacer.DEFAULT_PACING,
                            nowMs: NOW_MS,
                            staleAfterMs: 30 * MINUTE,
                          }),
                        ),
                },
          );
    const dependencies = Layer.mergeAll(
      NodeServices.layer,
      NodeCrypto.layer,
      Layer.mock(ThreadManagementService.ThreadManagementService)({
        hasReceipt: () => Effect.succeed(input.retryOfAccepted === true),
        getThreadRecords: (threadId) =>
          Effect.succeed(
            threadId === parentThreadId
              ? parentProjection(dispatched.length > 0 ? [task] : [])
              : childProjection,
          ),
        isProviderInstanceEnabledForProject: ({ instanceId }) =>
          Effect.succeed(enabled(instanceId)),
        assertProviderInstanceEnabled: ({ projectId, instanceId }) =>
          enabled(instanceId)
            ? Effect.void
            : Effect.fail(
                new ThreadManagementService.ThreadManagementProviderInstanceDisabledError({
                  projectId,
                  instanceId,
                }),
              ),
        dispatch: (command) =>
          Effect.sync(() => {
            dispatched.push(command as unknown as Dispatched);
            return {
              sequence: 1,
              storedEvents: [{ event: { type: "subagent.updated", payload: task } }],
            } as never;
          }),
      }),
      Layer.mock(ProviderRegistry.ProviderRegistry)({
        getProviders: Effect.succeed(input.providers),
        refreshInstance: () => Effect.succeed(input.providers),
      }),
      Layer.succeed(
        ProviderAdapterRegistry.ProviderAdapterRegistryV2,
        ProviderAdapterRegistry.ProviderAdapterRegistryV2.of({
          list: () => Effect.succeed(input.providers.map((entry) => entry.instanceId)),
          get: (instanceId) =>
            Effect.succeed({
              instanceId,
            } as unknown as ProviderAdapter.ProviderAdapterV2["Service"]),
        }),
      ),
      Layer.mock(ProjectService.ProjectService)({}),
      Layer.mock(SecretRequests.SecretRequests)({}),
      Layer.mock(RemoteDelegation.RemoteDelegation)({}),
      Layer.mock(ScheduledTaskService.ScheduledTaskService)({}),
      Layer.mock(ThreadLaunchService.ThreadLaunchService)({}),
      Layer.mock(GitVcsDriver.GitVcsDriver)({}),
      ServerSettings.layerTest(input.settings ?? {}),
      layerPacer,
    );
    const result = yield* Effect.gen(function* () {
      const service = yield* OrchestratorMcpService.OrchestratorMcpService;
      return yield* service
        .delegateTask(scope, {
          task: "Review the change.",
          ...(input.target === undefined ? {} : { target: input.target }),
          mode: "async",
          clientRequestId: "usage-gate-1",
        })
        .pipe(Effect.result);
    }).pipe(Effect.provide(OrchestratorMcpService.layer.pipe(Layer.provide(dependencies))));
    return { result, dispatched };
  });

const enforce = { usagePacingGate: "enforce" } as const;

describe("OrchestratorMcpService usage pacing gate", () => {
  it.effect("off (the default): no pace is read, even when the account is full", () =>
    Effect.gen(function* () {
      for (const settings of [undefined, { usagePacingGate: "off" } as const]) {
        const { result, dispatched } = yield* delegate({
          ...(settings === undefined ? {} : { settings }),
          providers: [main(100), second(10)],
          pacer: "untouched",
        });
        assert.isTrue(result._tag === "Success");
        if (result._tag !== "Success") return;
        assert.isUndefined(result.success.usagePacingNote);
        assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: MAIN, model: MODEL });
      }
    }),
  );

  it.effect("enforce, room: the task runs as asked, without a note", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: enforce,
        providers: [main(40), second(10)],
      });
      assert.isTrue(result._tag === "Success");
      if (result._tag !== "Success") return;
      assert.isFalse("usagePacingNote" in result.success);
      assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: MAIN, model: MODEL });
    }),
  );

  it.effect("enforce, ahead: the task runs on the account with room, and the reply says so", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: enforce,
        providers: [main(60), second(10)],
      });
      assert.isTrue(result._tag === "Success");
      if (result._tag !== "Success") return;
      assert.include(result.success.usagePacingNote ?? "", "runs on codex-second");
      assert.strictEqual(dispatched.length, 1);
      assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: SECOND, model: MODEL });
    }),
  );

  it.effect("enforce, ahead: the caller's options move with the task", () =>
    Effect.gen(function* () {
      const { dispatched } = yield* delegate({
        settings: enforce,
        providers: [main(60), second(10)],
        target: { options: [{ id: "reasoningEffort", value: "low" }] },
      });
      assert.deepStrictEqual(dispatched[0]?.modelSelection, {
        instanceId: SECOND,
        model: MODEL,
        options: [{ id: "reasoningEffort", value: "low" }],
      });
    }),
  );

  it.effect("enforce, ahead: an instance the caller named is kept; the task is held", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: enforce,
        providers: [main(60), second(10)],
        target: { providerInstanceId: MAIN },
      });
      assert.isTrue(result._tag === "Failure");
      if (result._tag !== "Failure") return;
      assert.strictEqual(result.failure.code, "usage_pacing_hold");
      assert.include(result.failure.message, "codex-second has room");
      assert.deepStrictEqual(dispatched, []);
    }),
  );

  it.effect("enforce, ahead: never moves to an account the project turned off", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: enforce,
        providers: [main(60), second(10)],
        disabledForProject: [SECOND],
      });
      assert.isTrue(result._tag === "Failure");
      if (result._tag !== "Failure") return;
      assert.strictEqual(result.failure.code, "usage_pacing_hold");
      assert.include(result.failure.message, "No other account of this provider has room.");
      assert.deepStrictEqual(dispatched, []);
    }),
  );

  it.effect(
    "enforce, ahead: never moves to an account that would run in another runtime mode",
    () =>
      Effect.gen(function* () {
        const { result, dispatched } = yield* delegate({
          settings: enforce,
          providers: [main(60), second(10, ["approval-required"])],
        });
        assert.isTrue(result._tag === "Failure");
        if (result._tag !== "Failure") return;
        assert.strictEqual(result.failure.code, "usage_pacing_hold");
        assert.include(result.failure.message, "No other account of this provider has room.");
        assert.deepStrictEqual(dispatched, []);
      }),
  );

  it.effect("enforce, ahead, nowhere to go: held with a time, and nothing is started", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: enforce,
        providers: [main(60), second(70)],
      });
      assert.isTrue(result._tag === "Failure");
      if (result._tag !== "Failure") return;
      assert.strictEqual(result.failure.code, "usage_pacing_hold");
      assert.include(result.failure.message, `clears at ${at(505 * MINUTE)} at the earliest`);
      assert.deepStrictEqual(dispatched, []);
    }),
  );

  it.effect("enforce, over the even pace: runs at the effort the owner listed", () =>
    Effect.gen(function* () {
      const settings = { ...enforce, usagePacingLowerEffort: { high: "medium" } };
      const byDefault = yield* delegate({ settings, providers: [main(60)] });
      assert.deepStrictEqual(byDefault.dispatched[0]?.modelSelection, {
        instanceId: MAIN,
        model: MODEL,
        options: [{ id: "reasoningEffort", value: "medium" }],
      });
      assert.isTrue(byDefault.result._tag === "Success");
      if (byDefault.result._tag !== "Success") return;
      assert.include(byDefault.result.success.usagePacingNote ?? "", "medium instead of high");

      const asked = yield* delegate({
        settings,
        providers: [main(60)],
        target: { options: [{ id: "reasoningEffort", value: "high" }] },
      });
      assert.deepStrictEqual(asked.dispatched[0]?.modelSelection.options, [
        { id: "reasoningEffort", value: "medium" },
      ]);

      const full = yield* delegate({ settings, providers: [main(100)] });
      assert.isTrue(full.result._tag === "Failure");
      assert.deepStrictEqual(full.dispatched, []);
    }),
  );

  it.effect("enforce, unknown pace: the task runs as asked, with a note", () =>
    Effect.gen(function* () {
      const stale = provider(MAIN, "main@example.com", []);
      for (const [providers, pacer, text] of [
        [[stale, second(10)], "real", "is not known (emptyRead)"],
        [[main(100), second(10)], "failing", "could not be read"],
        [[main(100), second(10)], "absent", "could not be read"],
      ] as const) {
        const { result, dispatched } = yield* delegate({ settings: enforce, providers, pacer });
        assert.isTrue(result._tag === "Success", text);
        if (result._tag !== "Success") return;
        assert.include(result.success.usagePacingNote ?? "", text);
        assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: MAIN, model: MODEL });
      }
    }),
  );

  it.effect("advise: nothing changes; the reply says what enforce would do", () =>
    Effect.gen(function* () {
      const advise = { usagePacingGate: "advise" } as const;
      for (const [providers, text] of [
        [[main(60), second(10)], "would run this task there"],
        [[main(100)], "would hold this task"],
      ] as const) {
        const { result, dispatched } = yield* delegate({ settings: advise, providers });
        assert.isTrue(result._tag === "Success", text);
        if (result._tag !== "Success") return;
        assert.include(result.success.usagePacingNote ?? "", text);
        assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: MAIN, model: MODEL });
      }
    }),
  );

  it.effect("a retry of a delegation that was accepted is not paced again", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: enforce,
        providers: [main(100)],
        pacer: "untouched",
        retryOfAccepted: true,
      });
      assert.isTrue(result._tag === "Success");
      assert.strictEqual(dispatched.length, 1);
    }),
  );
});
