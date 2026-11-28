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
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Ref from "effect/Ref";
import type * as Result from "effect/Result";
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

const main = (usedPercent: number, modes?: ReadonlyArray<RuntimeMode>) =>
  provider(MAIN, "main@example.com", [weekly(usedPercent)], modes);
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

const parentProjection = (
  subagents: ReadonlyArray<unknown>,
  options?: ReadonlyArray<{ readonly id: string; readonly value: string | boolean }>,
): OrchestrationV2ThreadProjection => {
  const modelSelection = { instanceId: MAIN, model: MODEL, ...(options ? { options } : {}) };
  return {
    thread: {
      id: parentThreadId,
      projectId,
      title: "Parent",
      createdBy: "user",
      creationSource: "web",
      modelSelection,
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
        modelSelection,
      },
    ],
    contextTransfers: [],
    subagents,
  } as unknown as OrchestrationV2ThreadProjection;
};

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
    Pick<
      ServerSettingsValue,
      "usagePacingGate" | "usagePacingLowerEffort" | "modelPolicy" | "accountOrder"
    >
  >;
  readonly providers: ReadonlyArray<ServerProvider>;
  readonly pacer?: "real" | "untouched" | "absent" | "failing";
  readonly target?: OrchestratorMcpTarget;
  readonly disabledForProject?: ReadonlyArray<ProviderInstanceId>;
  readonly retryOfAccepted?: boolean;
  /** The options the parent thread runs with (none by default). */
  readonly parentOptions?: ReadonlyArray<{ readonly id: string; readonly value: string }>;
  readonly runtimeMode?: RuntimeMode;
  readonly settingsUnreadable?: boolean;
  /** The parent project's folder; without it the project is not found. */
  readonly workspaceRoot?: string;
  /** What the registry holds once the folder is scanned (the folder's own provider config). */
  readonly scanned?: ReadonlyArray<ServerProvider>;
  /** A scan that fails: the registry recovers to the list as it was, as the real one does. */
  readonly scanFails?: (instanceId: ProviderInstanceId, nthScan: number, cwd: string) => boolean;
  /**
   * One call per entry, on the same service: at that many ms after NOW_MS, or with the parent's
   * folder for that call, what another path (the UI, say) published just before it, and a
   * settings change the owner made just before it. Default: one call.
   */
  readonly calls?: ReadonlyArray<
    | number
    | {
        readonly atMs: number;
        readonly workspaceRoot?: string;
        readonly publish?: ReadonlyArray<ServerProvider>;
        readonly settings?: Partial<Pick<ServerSettingsValue, "usagePacingGate">>;
      }
  >;
}) =>
  Effect.gen(function* () {
    // The unreadable settings mock has no `updateSettings`: a settings change there would die on
    // the mock with no hint why. Refuse the combination at once, with the reason.
    if (
      input.settingsUnreadable === true &&
      input.calls?.some((call) => typeof call !== "number" && call.settings !== undefined)
    ) {
      return yield* Effect.die(
        new Error(
          "delegate(): settingsUnreadable cannot be combined with a settings change in calls (unreadable settings cannot be changed)",
        ),
      );
    }
    yield* TestClock.setTime(NOW_MS);
    const dispatched: Array<Dispatched> = [];
    const scans: Array<{ readonly instanceId: ProviderInstanceId; readonly cwd: string }> = [];
    const registry = yield* Ref.make(input.providers);
    let workspaceRoot = input.workspaceRoot;
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
              ? parentProjection(dispatched.length > 0 ? [task] : [], input.parentOptions)
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
        getProviders: Ref.get(registry),
        refreshInstance: () => Ref.get(registry),
        // Like the real registry: a scan reads one account's config for one folder, and its
        // snapshot is dated by the scan.
        refreshWorkspaceSnapshot: ({ instanceId, cwd }) =>
          Effect.sync(() => scans.push({ instanceId, cwd })).pipe(
            Effect.flatMap((nthScan) =>
              input.scanFails?.(instanceId, nthScan, cwd) === true
                ? Ref.get(registry)
                : Clock.currentTimeMillis.pipe(
                    Effect.flatMap((nowMs) =>
                      Ref.updateAndGet(registry, (current) =>
                        current.map((provider) => {
                          if (provider.instanceId !== instanceId) return provider;
                          const scanned = (input.scanned ?? input.providers).find(
                            (entry) => entry.instanceId === instanceId,
                          );
                          if (scanned === undefined) return provider;
                          const checkedAt = DateTime.formatIso(DateTime.makeUnsafe(nowMs));
                          return {
                            ...scanned,
                            workspaceSnapshots: [
                              ...(provider.workspaceSnapshots ?? []).filter(
                                (snapshot) => snapshot.cwd !== cwd,
                              ),
                              ...(scanned.workspaceSnapshots ?? [])
                                .filter((snapshot) => snapshot.cwd === cwd)
                                .map((snapshot) => ({ ...snapshot, checkedAt })),
                            ],
                          };
                        }),
                      ),
                    ),
                  ),
            ),
          ),
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
      Layer.mock(ProjectService.ProjectService)({
        getById: () =>
          Effect.suspend(() =>
            workspaceRoot === undefined
              ? Effect.succeedNone
              : Effect.succeedSome({ id: projectId, workspaceRoot } as never),
          ),
        workspaceScanDirectory: (cwd) => Effect.succeed(cwd),
      }),
      Layer.mock(SecretRequests.SecretRequests)({}),
      Layer.mock(RemoteDelegation.RemoteDelegation)({}),
      Layer.mock(ScheduledTaskService.ScheduledTaskService)({}),
      Layer.mock(ThreadLaunchService.ThreadLaunchService)({}),
      Layer.mock(GitVcsDriver.GitVcsDriver)({}),
      input.settingsUnreadable === true
        ? Layer.mock(ServerSettings.ServerSettingsService)({
            getSettings: Effect.fail(
              new ServerSettingsError({ settingsPath: "settings.json", operation: "read-file" }),
            ),
          })
        : ServerSettings.layerTest(input.settings ?? {}),
      layerPacer,
    );
    const results = yield* Effect.gen(function* () {
      const service = yield* OrchestratorMcpService.OrchestratorMcpService;
      const serverSettings = yield* ServerSettings.ServerSettingsService;
      return yield* Effect.forEach(input.calls ?? [0], (call, index) =>
        Effect.gen(function* () {
          const { atMs, ...rest } = typeof call === "number" ? { atMs: call } : call;
          if ("workspaceRoot" in rest) workspaceRoot = rest.workspaceRoot;
          if (rest.publish !== undefined) yield* Ref.set(registry, rest.publish);
          if (rest.settings !== undefined) {
            yield* serverSettings.updateSettings(rest.settings).pipe(Effect.orDie);
          }
          yield* TestClock.setTime(NOW_MS + atMs);
        }).pipe(
          Effect.andThen(
            service.delegateTask(scope, {
              task: "Review the change.",
              ...(input.target === undefined ? {} : { target: input.target }),
              ...(input.runtimeMode === undefined ? {} : { runtimeMode: input.runtimeMode }),
              mode: "async",
              clientRequestId: `usage-gate-${index + 1}`,
            }),
          ),
          Effect.result,
        ),
      );
    }).pipe(Effect.provide(OrchestratorMcpService.layer.pipe(Layer.provideMerge(dependencies))));
    return { result: results.at(-1)!, results, dispatched, scans };
  });

const enforce = { usagePacingGate: "enforce" } as const;

const ROOT = "/work/project";
const LOCAL = "local/coder";

/** The provider as its folder scan sees it: the project's own config adds `models`. */
const inFolder = (
  base: ServerProvider,
  models: ServerProvider["models"] = [
    ...base.models,
    { ...base.models[0]!, slug: LOCAL, name: LOCAL },
  ],
): ServerProvider => ({
  ...base,
  workspaceSnapshots: [
    { cwd: ROOT, checkedAt: at(-MINUTE), slashCommands: [], skills: [], models },
  ],
});

const OTHER_ROOT = "/work/other";
const THIRD = ProviderInstanceId.make("codex-third");
const third = (usedPercent: number) => provider(THIRD, "third@example.com", [weekly(usedPercent)]);

/** A provider with a snapshot of each folder, as its scan of that folder sees it. */
const inFolders = (
  base: ServerProvider,
  folders: ReadonlyArray<{
    readonly cwd: string;
    readonly models?: ServerProvider["models"];
    readonly slashCommandsPending?: boolean;
    readonly modelsPending?: boolean;
  }>,
): ServerProvider => ({
  ...base,
  workspaceSnapshots: folders.map(({ cwd, models, ...pending }) => ({
    cwd,
    checkedAt: at(-MINUTE),
    slashCommands: [],
    skills: [],
    models: models ?? inFolder(base).workspaceSnapshots![0]!.models,
    ...pending,
  })),
});

/** The hold message or the note of one call. */
const textOf = (
  result: Result.Result<
    { readonly usagePacingNote?: string | undefined },
    { readonly message: string }
  >,
) => (result._tag === "Failure" ? result.failure.message : (result.success.usagePacingNote ?? ""));

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

  it.effect(
    "enforce, ahead: under a model policy, a moved pick that names its model takes none of the parent's options",
    () =>
      Effect.gen(function* () {
        // An OpenCode parent that runs an agent and a level; the call names the model and no option,
        // so with a model policy on neither reaches the child, on the account the pacer moves the
        // task to as well. With the policy off the move keeps what upstream's lookup gives it.
        const opencode = (base: ServerProvider): ServerProvider => ({
          ...base,
          driver: ProviderDriverKind.make("opencode"),
          models: base.models.map((model) => ({
            ...model,
            capabilities: {
              optionDescriptors: [
                ...(model.capabilities?.optionDescriptors ?? []),
                {
                  id: "agent",
                  label: "Agent",
                  type: "select",
                  options: [
                    { id: "build", label: "Build" },
                    { id: "plan", label: "Plan" },
                  ],
                },
              ],
            },
          })),
        });
        const pick = (mode: "off" | "advise") =>
          delegate({
            settings: { ...enforce, modelPolicy: { mode, allow: [] } },
            providers: [opencode(main(60)), opencode(second(10))],
            parentOptions: [
              { id: "reasoningEffort", value: "low" },
              { id: "agent", value: "plan" },
            ],
            target: { model: MODEL },
          });
        const { result, dispatched } = yield* pick("advise");
        assert.include(textOf(result), "runs on codex-second");
        assert.deepStrictEqual(dispatched[0]?.modelSelection, {
          instanceId: SECOND,
          model: MODEL,
          options: [],
        });
        const off = yield* pick("off");
        assert.include(textOf(off.result), "runs on codex-second");
        assert.deepStrictEqual(off.dispatched[0]?.modelSelection, {
          instanceId: SECOND,
          model: MODEL,
          options: [
            { id: "reasoningEffort", value: "low" },
            { id: "agent", value: "plan" },
          ],
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
      assert.include(
        result.failure.message,
        "No other account of this provider can run it (codex-second has room, but this project turned it off).",
      );
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
        assert.include(
          result.failure.message,
          "(codex-second has room, but it would run the task in another runtime mode).",
        );
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

  it.effect("enforce: a request that is invalid anyway fails at once, not after a hold", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: enforce,
        providers: [main(60, ["approval-required"]), second(70)],
        runtimeMode: "full-access",
        pacer: "untouched",
      });
      assert.isTrue(result._tag === "Failure");
      if (result._tag !== "Failure") return;
      assert.strictEqual(result.failure.code, "invalid_request");
      assert.deepStrictEqual(dispatched, []);
    }),
  );

  // The pacer never reads a pace then: the model policy, which cannot be checked either, refuses
  // first (fail closed, fork/modelPolicyCheck.ts), and nothing is dispatched.
  it.effect("settings that cannot be read: the model policy refuses before the pacer reads", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        providers: [main(100), second(10)],
        pacer: "untouched",
        settingsUnreadable: true,
      });
      assert.isTrue(result._tag === "Failure");
      if (result._tag !== "Failure") return;
      assert.strictEqual(result.failure.code, "model_unavailable");
      assert.include(result.failure.message, "the server settings cannot be read");
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

  // The model policy checks what runs, too: the agent asked for an effort its row allows, and the
  // pacer would lower it to one the row does not. The agent cannot fix that pick, so the policy
  // wins: the task runs at its checked effort, with a warning naming both settings
  // (fork/delegateChecks.ts).
  it.effect(
    "enforce, over the even pace: an effort the pacer lowers off the policy's row keeps the checked one",
    () =>
      Effect.gen(function* () {
        const row = {
          driver: "codex",
          model: MODEL,
          efforts: ["high"],
          defaultEffort: "high",
          useFor: "paced work",
        };
        const target = { model: MODEL, options: [{ id: "reasoningEffort", value: "high" }] };
        const logs: Array<string> = [];
        const lowered = yield* delegate({
          settings: {
            ...enforce,
            usagePacingLowerEffort: { high: "medium" },
            modelPolicy: { mode: "enforce", allow: [row] },
          } as never,
          providers: [main(60)],
          target: target as never,
        }).pipe(
          Effect.provide(
            Logger.layer([Logger.make((entry) => logs.push(Logger.formatLogFmt.log(entry)))], {
              mergeWithExisting: false,
            }),
          ),
        );
        assert.isTrue(lowered.result._tag === "Success");
        if (lowered.result._tag !== "Success") return;
        // The reply keeps the row's note, not the pacer's (which names the effort it did not use).
        assert.strictEqual(
          lowered.result.success.usagePacingNote,
          `Model policy: ${MODEL} is for paced work.`,
        );
        assert.deepStrictEqual(lowered.dispatched[0]?.modelSelection.options, [
          { id: "reasoningEffort", value: "high" },
        ]);
        const warning = logs.filter((line) => line.includes("the task keeps its checked effort"));
        assert.lengthOf(warning, 1);
        assert.include(warning[0], "usagePacingLowerEffort lowered an effort that modelPolicy");
        assert.include(warning[0], `${MODEL} does not allow effort medium`);
        // With the lower level in the row it runs there; under the pace it runs as asked.
        const allowed = yield* delegate({
          settings: {
            ...enforce,
            usagePacingLowerEffort: { high: "medium" },
            modelPolicy: { mode: "enforce", allow: [{ ...row, efforts: ["medium", "high"] }] },
          } as never,
          providers: [main(60)],
          target: target as never,
        });
        assert.deepStrictEqual(allowed.dispatched[0]?.modelSelection.options, [
          { id: "reasoningEffort", value: "medium" },
        ]);
        // Only a lowered effort keeps the checked pick: a move to an account whose model marks
        // another option as its reasoning control (read as a second effort there) is still refused.
        const level = { id: "level", label: "Level", type: "select", options: [{ id: "max" }] };
        const withLevel = (base: ServerProvider, role?: "reasoning") =>
          ({
            ...base,
            models: base.models.map((model) => ({
              ...model,
              capabilities: {
                optionDescriptors: [
                  ...(model.capabilities?.optionDescriptors ?? []),
                  { ...level, ...(role === undefined ? {} : { role }) },
                ],
              },
            })),
          }) as ServerProvider;
        const moved = yield* delegate({
          settings: { ...enforce, modelPolicy: { mode: "enforce", allow: [row] } } as never,
          providers: [withLevel(main(60)), withLevel(second(10), "reasoning")],
          target: { model: MODEL, options: [...target.options, { id: "level", value: "max" }] },
        });
        assert.isTrue(moved.result._tag === "Failure");
        if (moved.result._tag !== "Failure") return;
        assert.include(moved.result.failure.message, `${MODEL} does not allow effort high + max`);
        assert.deepStrictEqual(moved.dispatched, []);
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

  it.effect("enforce, ahead: moves a model only the project's own config adds (#559, #568)", () =>
    Effect.gen(function* () {
      const providers = [inFolder(main(60)), inFolder(second(10))];
      // The registry before the call. A scan reads one account's folder config: the
      // lookup scans only the requested account, so the gate scans the other one itself.
      for (const [registry, scanned] of [
        // Both accounts scanned the folder before: nothing is scanned again.
        [providers, []],
        // Neither has: the lookup scans the requested one, the gate the one with room.
        [
          [main(60), second(10)],
          [MAIN, SECOND],
        ],
        // Only the requested one has (a fresh scan of it dropped the other's snapshot).
        [[inFolder(main(60)), second(10)], [SECOND]],
      ] as const) {
        const { result, dispatched, scans } = yield* delegate({
          settings: enforce,
          providers: registry,
          scanned: providers,
          workspaceRoot: ROOT,
          target: { model: LOCAL },
        });
        assert.isTrue(result._tag === "Success", `scans ${scanned.join(", ")}`);
        if (result._tag !== "Success") return;
        assert.include(result.success.usagePacingNote ?? "", "runs on codex-second");
        assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: SECOND, model: LOCAL });
        assert.deepStrictEqual(
          scans,
          scanned.map((instanceId) => ({ instanceId, cwd: ROOT })),
        );
      }
    }),
  );

  it.effect(
    "enforce, ahead: an account whose folder scan does not list the model is named, not 'no room' (#568)",
    () =>
      Effect.gen(function* () {
        // The other account's own config for this folder adds nothing (another login, say).
        const scanned = [inFolder(main(60)), inFolder(second(10), second(10).models)];
        for (const registry of [[inFolder(main(60)), second(10)], scanned]) {
          const { result, dispatched, scans } = yield* delegate({
            settings: enforce,
            providers: registry,
            scanned,
            workspaceRoot: ROOT,
            target: { model: LOCAL },
          });
          assert.isTrue(result._tag === "Failure");
          if (result._tag !== "Failure") return;
          assert.strictEqual(result.failure.code, "usage_pacing_hold");
          assert.include(
            result.failure.message,
            `No other account of this provider can run it (codex-second has room, but it does not list ${LOCAL} for this folder).`,
          );
          assert.notInclude(result.failure.message, "has room.");
          assert.deepStrictEqual(dispatched, []);
          // Scanned once per call when the model is missing; a scan that adds nothing is final.
          assert.deepStrictEqual(scans, [{ instanceId: SECOND, cwd: ROOT }]);
        }

        // With a lower effort allowed, the task runs on its own account at that effort, and the
        // note says why it did not move.
        const lower = yield* delegate({
          settings: { ...enforce, usagePacingLowerEffort: { high: "medium" } },
          providers: scanned,
          scanned,
          workspaceRoot: ROOT,
          target: { model: LOCAL },
        });
        assert.isTrue(lower.result._tag === "Success");
        if (lower.result._tag !== "Success") return;
        assert.include(
          lower.result.success.usagePacingNote ?? "",
          "codex-second has room, but it does not list",
        );
        assert.deepStrictEqual(lower.dispatched[0]?.modelSelection, {
          instanceId: MAIN,
          model: LOCAL,
          options: [{ id: "reasoningEffort", value: "medium" }],
        });
      }),
  );

  it.effect("enforce, ahead: a candidate refused for another reason is not scanned (#596)", () =>
    Effect.gen(function* () {
      // The other account is unhealthy: its folder could not change that, so it is not scanned.
      const { result, dispatched, scans } = yield* delegate({
        settings: enforce,
        providers: [inFolder(main(60)), { ...second(10), status: "error" }],
        scanned: [inFolder(main(60)), inFolder(second(10))],
        workspaceRoot: ROOT,
        target: { model: LOCAL },
      });
      assert.isTrue(result._tag === "Failure");
      if (result._tag !== "Failure") return;
      assert.include(
        result.failure.message,
        "(codex-second has room, but the task cannot run there (provider_unavailable)).",
      );
      assert.deepStrictEqual(dispatched, []);
      assert.deepStrictEqual(scans, []);
    }),
  );

  it.effect(
    "enforce, ahead: a failed folder scan says so and is not run on every call (#596)",
    () =>
      Effect.gen(function* () {
        const scanned = [inFolder(main(60)), inFolder(second(10))];
        const failedText = (retry: string) =>
          `(codex-second has room, but it does not list ${LOCAL} and its scan of this folder gave no result (scanned again after ${retry})).`;
        // The first two scans fail, the third works. Calls: now, a minute later (backing off), just
        // before the back-off ends, once it ends (scans and fails again), and after that (works).
        const { results, dispatched, scans } = yield* delegate({
          settings: enforce,
          providers: [inFolder(main(60)), second(10)],
          scanned,
          scanFails: (instanceId, nthScan) => instanceId === SECOND && nthScan <= 2,
          workspaceRoot: ROOT,
          target: { model: LOCAL },
          calls: [0, MINUTE, 5 * MINUTE - 1, 5 * MINUTE, 10 * MINUTE],
        });
        const messages = results.map((result) =>
          result._tag === "Failure" ? result.failure.message : "",
        );
        for (const message of messages.slice(0, 3)) {
          assert.include(message, failedText("2026-11-13T12:05:00.000Z"));
          assert.notInclude(message, "for this folder");
        }
        assert.include(messages[3]!, failedText("2026-11-13T12:10:00.000Z"));
        // Scanned at the first call, at the end of the back-off and after the second one: 3 scans.
        assert.deepStrictEqual(
          scans,
          [SECOND, SECOND, SECOND].map((instanceId) => ({ instanceId, cwd: ROOT })),
        );
        // The scan that worked moves the task; nothing ran before it.
        const last = results[4]!;
        assert.isTrue(last._tag === "Success");
        if (last._tag !== "Success") return;
        assert.include(last.success.usagePacingNote ?? "", "runs on codex-second");
        assert.deepStrictEqual(
          dispatched.map((entry) => entry.modelSelection),
          [{ instanceId: SECOND, model: LOCAL }],
        );
      }),
  );

  it.effect(
    "enforce, ahead: an old snapshot and a failed scan are not read as an answer (#596)",
    () =>
      Effect.gen(function* () {
        // The other account scanned this folder long ago (before its config added the model).
        const old: ServerProvider = {
          ...second(10),
          workspaceSnapshots: [
            {
              cwd: ROOT,
              checkedAt: at(-DAY),
              slashCommands: [],
              skills: [],
              models: second(10).models,
            },
          ],
        };
        for (const [fails, text] of [
          [true, `it does not list ${LOCAL} and its scan of this folder gave no result`],
          [false, "runs on codex-second"],
        ] as const) {
          const { result, scans } = yield* delegate({
            settings: enforce,
            providers: [inFolder(main(60)), old],
            scanned: [inFolder(main(60)), inFolder(second(10))],
            scanFails: () => fails,
            workspaceRoot: ROOT,
            target: { model: LOCAL },
          });
          const text_ =
            result._tag === "Failure"
              ? result.failure.message
              : (result.success.usagePacingNote ?? "");
          assert.include(text_, text);
          assert.deepStrictEqual(scans, [{ instanceId: SECOND, cwd: ROOT }]);
        }
      }),
  );

  it.effect(
    "enforce, ahead: a current answer published during a back-off is the reason given (#616)",
    () =>
      Effect.gen(function* () {
        // The scan fails; a minute later the UI publishes a current scan without the model.
        const { results, scans } = yield* delegate({
          settings: enforce,
          providers: [inFolder(main(60)), second(10)],
          scanFails: () => true,
          workspaceRoot: ROOT,
          target: { model: LOCAL },
          calls: [
            0,
            {
              atMs: MINUTE,
              publish: [inFolder(main(60)), inFolder(second(10), second(10).models)],
            },
          ],
        });
        assert.include(textOf(results[0]!), "its scan of this folder gave no result");
        assert.include(
          textOf(results[1]!),
          `(codex-second has room, but it does not list ${LOCAL} for this folder).`,
        );
        assert.notInclude(textOf(results[1]!), "gave no result");
        // Still within the back-off: no second scan.
        assert.deepStrictEqual(scans, [{ instanceId: SECOND, cwd: ROOT }]);
      }),
  );

  it.effect(
    "enforce, ahead: a scan pending only for slash commands lists real models; any pending scan backs off (#616)",
    () =>
      Effect.gen(function* () {
        for (const [pending, models, text, moves] of [
          // Its model list is an answer: the reply says the model is not listed.
          [
            { slashCommandsPending: true },
            second(10).models,
            `it does not list ${LOCAL} for this folder`,
            false,
          ],
          // Its models are pending: no answer yet.
          [
            { modelsPending: true },
            second(10).models,
            `it does not list ${LOCAL} and its scan of this folder gave no result (scanned again after 2026-11-13T12:05:00.000Z)`,
            false,
          ],
          // A list pending only for slash commands that has the model: the task moves.
          [{ slashCommandsPending: true }, undefined, "runs on codex-second", true],
        ] as const) {
          const { results, scans, dispatched } = yield* delegate({
            settings: enforce,
            providers: [inFolder(main(60)), second(10)],
            scanned: [
              inFolder(main(60)),
              inFolders(second(10), [
                { cwd: ROOT, ...(models === undefined ? {} : { models }), ...pending },
              ]),
            ],
            workspaceRoot: ROOT,
            target: { model: LOCAL },
            calls: moves ? [0] : [0, MINUTE],
          });
          for (const result of results) assert.include(textOf(result), text);
          if (!moves) {
            for (const result of results) assert.notInclude(textOf(result), "has room.");
            assert.deepStrictEqual(dispatched, []);
          }
          // The registry rescans a pending snapshot on every call: one scan in two calls.
          assert.deepStrictEqual(scans, [{ instanceId: SECOND, cwd: ROOT }]);
        }
      }),
  );

  it.effect("enforce, ahead: a clock that goes back does not stretch the back-off (#616)", () =>
    Effect.gen(function* () {
      const { results, scans } = yield* delegate({
        settings: enforce,
        providers: [inFolder(main(60)), second(10)],
        scanFails: () => true,
        workspaceRoot: ROOT,
        target: { model: LOCAL },
        // An hour back, then a minute on from there.
        calls: [0, -60 * MINUTE, -59 * MINUTE],
      });
      assert.include(textOf(results[0]!), "(scanned again after 2026-11-13T12:05:00.000Z)");
      // The back-off would otherwise run 65 minutes: it ends, and the next one starts from now.
      assert.include(textOf(results[1]!), "(scanned again after 2026-11-13T11:05:00.000Z)");
      assert.include(textOf(results[2]!), "(scanned again after 2026-11-13T11:05:00.000Z)");
      assert.deepStrictEqual(
        scans,
        [SECOND, SECOND].map((instanceId) => ({ instanceId, cwd: ROOT })),
      );
    }),
  );

  it.effect("enforce, ahead: the back-off is per folder (#616)", () =>
    Effect.gen(function* () {
      const both = [{ cwd: ROOT }, { cwd: OTHER_ROOT }];
      // The other account's scan of ROOT fails; its scan of the other folder works.
      const { results, scans, dispatched } = yield* delegate({
        settings: enforce,
        providers: [inFolders(main(60), both), second(10)],
        scanned: [inFolders(main(60), both), inFolders(second(10), both)],
        scanFails: (_instanceId, _nthScan, cwd) => cwd === ROOT,
        target: { model: LOCAL },
        calls: [
          { atMs: 0, workspaceRoot: ROOT },
          { atMs: MINUTE, workspaceRoot: ROOT },
          { atMs: 2 * MINUTE, workspaceRoot: OTHER_ROOT },
        ],
      });
      assert.include(textOf(results[0]!), "gave no result");
      assert.include(textOf(results[1]!), "gave no result");
      // ROOT backs off; the other folder is still scanned, and the task moves there.
      assert.include(textOf(results[2]!), "runs on codex-second");
      assert.deepStrictEqual(scans, [
        { instanceId: SECOND, cwd: ROOT },
        { instanceId: SECOND, cwd: OTHER_ROOT },
      ]);
      assert.deepStrictEqual(
        dispatched.map((entry) => entry.modelSelection),
        [{ instanceId: SECOND, model: LOCAL }],
      );
    }),
  );

  it.effect("enforce, ahead: the back-off is per account (#616)", () =>
    Effect.gen(function* () {
      // Two accounts with room; the one with more room fails its scan of the folder.
      const { results, scans, dispatched } = yield* delegate({
        settings: enforce,
        providers: [inFolder(main(60)), second(10), third(20)],
        scanned: [inFolder(main(60)), inFolder(second(10)), inFolder(third(20))],
        scanFails: (instanceId) => instanceId === SECOND,
        workspaceRoot: ROOT,
        target: { model: LOCAL },
        calls: [0, MINUTE],
      });
      // The other account is still scanned, the same minute, and the task moves there.
      assert.include(textOf(results[0]!), "runs on codex-third");
      assert.include(textOf(results[1]!), "runs on codex-third");
      // The second call: codex-second backs off, codex-third has its answer.
      assert.deepStrictEqual(scans, [
        { instanceId: SECOND, cwd: ROOT },
        { instanceId: THIRD, cwd: ROOT },
      ]);
      assert.deepStrictEqual(
        dispatched.map((entry) => entry.modelSelection.instanceId),
        [THIRD, THIRD],
      );
    }),
  );

  it.effect("advise scans no folder; a named instance in enforce still does (#596)", () =>
    Effect.gen(function* () {
      const advise = { usagePacingGate: "advise" } as const;
      const scanned = [inFolder(main(60)), inFolder(second(10))];
      // advise, the other account has not scanned the folder: the note says it is not known.
      const unscanned = yield* delegate({
        settings: advise,
        providers: [inFolder(main(60)), second(10)],
        scanned,
        workspaceRoot: ROOT,
        target: { model: LOCAL },
      });
      assert.isTrue(unscanned.result._tag === "Success");
      if (unscanned.result._tag !== "Success") return;
      assert.include(
        unscanned.result.success.usagePacingNote ?? "",
        `codex-second has room, but it does not list ${LOCAL} and has no current scan of this folder (advice only, so none was run)`,
      );
      // It says that enforce would scan first, and what it would do either way.
      assert.include(
        unscanned.result.success.usagePacingNote ?? "",
        `"enforce" would first scan this folder for codex-second: if it lists the model and the task can run there, it would run this task on codex-second; if not, it would hold this task.`,
      );
      assert.deepStrictEqual(unscanned.scans, []);
      assert.deepStrictEqual(unscanned.dispatched[0]?.modelSelection, {
        instanceId: MAIN,
        model: LOCAL,
      });
      // advise, both scanned before: the advice is read from the snapshots, still without a scan.
      const known = yield* delegate({
        settings: advise,
        providers: scanned,
        workspaceRoot: ROOT,
        target: { model: LOCAL },
      });
      assert.isTrue(known.result._tag === "Success");
      if (known.result._tag !== "Success") return;
      assert.include(known.result.success.usagePacingNote ?? "", "would run this task there");
      assert.deepStrictEqual(known.scans, []);
      // enforce with the instance named: the scan decides between a hold and a lower effort.
      const pinned = yield* delegate({
        settings: { ...enforce, usagePacingLowerEffort: { high: "medium" } },
        providers: [inFolder(main(60)), second(10)],
        scanned,
        workspaceRoot: ROOT,
        target: { providerInstanceId: MAIN, model: LOCAL },
      });
      assert.isTrue(pinned.result._tag === "Failure");
      if (pinned.result._tag !== "Failure") return;
      assert.include(pinned.result.failure.message, "codex-second has room: pass it as");
      assert.deepStrictEqual(pinned.scans, [{ instanceId: SECOND, cwd: ROOT }]);
    }),
  );

  it.effect(
    "advise: a current list without the model is the reason; no scan is promised (#628)",
    () =>
      Effect.gen(function* () {
        // Another path (the UI, say) published a current scan of the folder that lacks the model.
        const { result, scans, dispatched } = yield* delegate({
          settings: { usagePacingGate: "advise" },
          providers: [inFolder(main(60)), inFolder(second(10), second(10).models)],
          workspaceRoot: ROOT,
          target: { model: LOCAL },
        });
        const note = textOf(result);
        assert.include(
          note,
          `codex-second has room, but it does not list ${LOCAL} for this folder`,
        );
        assert.include(note, '"enforce" would hold this task.');
        assert.notInclude(note, "would first scan");
        assert.notInclude(note, "no current scan");
        assert.deepStrictEqual(scans, []);
        assert.deepStrictEqual(
          dispatched.map((entry) => entry.modelSelection),
          [{ instanceId: MAIN, model: LOCAL }],
        );
      }),
  );

  it.effect(
    "advise right after enforce: a back-off enforce recorded holds in the advice (#628)",
    () =>
      Effect.gen(function* () {
        const advise = { usagePacingGate: "advise" } as const;
        // enforce scans and gets no answer; the owner switches to advise within the back-off, then
        // the back-off ends.
        const { results, scans } = yield* delegate({
          settings: enforce,
          providers: [inFolder(main(60)), second(10)],
          scanFails: () => true,
          workspaceRoot: ROOT,
          target: { model: LOCAL },
          calls: [
            0,
            { atMs: MINUTE, settings: advise },
            { atMs: 5 * MINUTE - 1 },
            { atMs: 5 * MINUTE },
          ],
        });
        const noResult = `it does not list ${LOCAL} and its scan of this folder gave no result (scanned again after 2026-11-13T12:05:00.000Z)`;
        assert.include(textOf(results[0]!), noResult);
        // Within the back-off enforce would not scan, so the advice does not say it would.
        for (const result of results.slice(1, 3)) {
          assert.include(textOf(result), noResult);
          assert.include(textOf(result), '"enforce" would hold this task.');
          assert.notInclude(textOf(result), "would first scan");
        }
        // Once it ends, enforce would scan again, and the advice says so (without a scan).
        assert.include(textOf(results[3]!), "no current scan of this folder (advice only");
        assert.include(
          textOf(results[3]!),
          '"enforce" would first scan this folder for codex-second',
        );
        assert.deepStrictEqual(scans, [{ instanceId: SECOND, cwd: ROOT }]);
      }),
  );

  it.effect(
    "advise: a list whose slash commands are still loading is scanned again by enforce, and the advice says so (#644)",
    () =>
      Effect.gen(function* () {
        const advise = { usagePacingGate: "advise" } as const;
        // Another path published a current scan of the folder that lacks the model, with its slash
        // commands still loading, and no back-off runs.
        const loading = inFolders(second(10), [
          { cwd: ROOT, models: second(10).models, slashCommandsPending: true },
        ]);
        const state = {
          providers: [inFolder(main(60)), loading],
          scanned: [inFolder(main(60)), loading],
          workspaceRoot: ROOT,
          target: { model: LOCAL },
        } as const;
        // enforce scans that folder first: the registry probes a snapshot that is not settled.
        const enforced = yield* delegate({ ...state, settings: enforce });
        assert.deepStrictEqual(enforced.scans, [{ instanceId: SECOND, cwd: ROOT }]);
        // advise, twice (runs again): it says enforce would scan first, and never scans itself.
        const advised = yield* delegate({ ...state, settings: advise, calls: [0, MINUTE] });
        for (const result of advised.results) {
          const note = textOf(result);
          assert.include(
            note,
            `codex-second has room, but it does not list ${LOCAL} for this folder in a scan that is still loading its slash commands (advice only, so it was not scanned again)`,
          );
          assert.include(
            note,
            `"enforce" would first scan this folder for codex-second: if it lists the model and the task can run there, it would run this task on codex-second; if not, it would hold this task.`,
          );
          assert.notInclude(note, '"enforce" would hold this task.');
        }
        assert.deepStrictEqual(advised.scans, []);
        assert.deepStrictEqual(
          advised.dispatched.map((entry) => entry.modelSelection),
          [MAIN, MAIN].map((instanceId) => ({ instanceId, model: LOCAL })),
        );
        // Right after enforce: its scan is still loading, so it backed off. Within the back-off
        // enforce answers from the list without a scan, and so does the advice.
        const afterEnforce = yield* delegate({
          ...state,
          settings: enforce,
          calls: [0, { atMs: MINUTE, settings: advise }],
        });
        assert.include(
          textOf(afterEnforce.results[0]!),
          `it does not list ${LOCAL} for this folder`,
        );
        const note = textOf(afterEnforce.results[1]!);
        assert.include(note, `it does not list ${LOCAL} for this folder`);
        assert.include(note, '"enforce" would hold this task.');
        assert.notInclude(note, "still loading");
        assert.notInclude(note, "would first scan");
        assert.deepStrictEqual(afterEnforce.scans, [{ instanceId: SECOND, cwd: ROOT }]);
      }),
  );

  it.effect("advise: a clock that goes back ends the back-off enforce recorded (#644)", () =>
    Effect.gen(function* () {
      const advise = { usagePacingGate: "advise" } as const;
      // enforce gets no answer and backs off; the owner switches to advise within it; then the
      // clock goes an hour back, which would stretch the back-off to 65 minutes.
      const { results, scans } = yield* delegate({
        settings: enforce,
        providers: [inFolder(main(60)), second(10)],
        scanFails: () => true,
        workspaceRoot: ROOT,
        target: { model: LOCAL },
        calls: [0, { atMs: MINUTE, settings: advise }, -60 * MINUTE, -59 * MINUTE],
      });
      const noResult = "gave no result (scanned again after 2026-11-13T12:05:00.000Z)";
      assert.include(textOf(results[0]!), noResult);
      assert.include(textOf(results[1]!), noResult);
      assert.include(textOf(results[1]!), '"enforce" would hold this task.');
      // The back-off ends, as it does in enforce: the advice says enforce would scan again.
      for (const result of results.slice(2)) {
        assert.include(textOf(result), "no current scan of this folder (advice only");
        assert.include(textOf(result), '"enforce" would first scan this folder for codex-second');
        assert.notInclude(textOf(result), "gave no result");
      }
      // advise wrote nothing: the only scan is enforce's.
      assert.deepStrictEqual(scans, [{ instanceId: SECOND, cwd: ROOT }]);
    }),
  );

  it.effect("the test helper refuses a settings change with unreadable settings (#644)", () =>
    Effect.gen(function* () {
      const exit = yield* delegate({
        settingsUnreadable: true,
        providers: [main(60)],
        calls: [0, { atMs: MINUTE, settings: enforce }],
      }).pipe(Effect.exit);
      assert.isTrue(exit._tag === "Failure");
      assert.include(String(exit), "settingsUnreadable cannot be combined with a settings change");
    }),
  );

  it.effect("enforce, over the even pace: lowers the effort of a project config model (#559)", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: { ...enforce, usagePacingLowerEffort: { high: "medium" } },
        providers: [inFolder(main(60))],
        workspaceRoot: ROOT,
        target: { model: LOCAL },
      });
      assert.isTrue(result._tag === "Success");
      if (result._tag !== "Success") return;
      assert.include(result.success.usagePacingNote ?? "", "medium instead of high");
      assert.deepStrictEqual(dispatched[0]?.modelSelection, {
        instanceId: MAIN,
        model: LOCAL,
        options: [{ id: "reasoningEffort", value: "medium" }],
      });
    }),
  );

  it.effect("no, an empty or an unscanned project config: paced as before (#559)", () =>
    Effect.gen(function* () {
      for (const [providers, workspaceRoot] of [
        [[main(60), second(10)], undefined],
        [[main(60), second(10)], ROOT],
        [[inFolder(main(60), main(60).models), inFolder(second(10), second(10).models)], ROOT],
      ] as const) {
        const { result, dispatched, scans } = yield* delegate({
          settings: enforce,
          providers,
          ...(workspaceRoot === undefined ? {} : { workspaceRoot }),
        });
        assert.isTrue(result._tag === "Success");
        if (result._tag !== "Success") return;
        assert.include(result.success.usagePacingNote ?? "", "runs on codex-second");
        assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: SECOND, model: MODEL });
        // A model every account lists needs no folder scan, whatever the folder holds.
        assert.deepStrictEqual(scans, []);
      }
      // A model nothing lists still fails as it did, and is not paced.
      const { result } = yield* delegate({
        settings: enforce,
        providers: [inFolder(main(60), main(60).models)],
        workspaceRoot: ROOT,
        target: { model: LOCAL },
        pacer: "untouched",
      });
      assert.isTrue(result._tag === "Failure");
      if (result._tag !== "Failure") return;
      assert.strictEqual(result.failure.code, "model_unavailable");
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

describe("OrchestratorMcpService account order", () => {
  /** Read two hours ago: past the 30-minute stale bound the pacer runs with here. */
  const stale = (usedPercent: number) => ({
    ...third(usedPercent),
    usageLimits: { checkedAt: at(-120 * MINUTE), windows: [weekly(usedPercent)] },
  });
  /** A full window whose reset time is a minute ago: the value belongs to the window before. */
  const resetPassed = (base: ServerProvider): ServerProvider => ({
    ...base,
    usageLimits: {
      checkedAt: at(-MINUTE),
      windows: [{ ...weekly(100), resetsAt: at(-MINUTE) }],
    },
  });
  const order = (...ids: ReadonlyArray<ProviderInstanceId>) => ({ accountOrder: ids });

  it.effect("account full: the task runs on the first account in the order with room", () =>
    Effect.gen(function* () {
      // The order wins over room: codex-second has more, but codex-third comes first.
      const { result, dispatched } = yield* delegate({
        settings: order(MAIN, THIRD, SECOND),
        providers: [main(100), second(10), third(50)],
      });
      assert.strictEqual(
        textOf(result),
        "Account order: codex is at its limit (Weekly 100% used), so this task runs on codex-third, the first account in the order with room.",
      );
      assert.strictEqual(dispatched.length, 1);
      assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: THIRD, model: MODEL });
    }),
  );

  it.effect("account with room: nothing moves and the reply has no note", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: order(MAIN, SECOND),
        providers: [main(90), second(10)],
      });
      assert.strictEqual(textOf(result), "");
      assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: MAIN, model: MODEL });
    }),
  );

  it.effect("account data stale: that account takes no work and the next one does", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: order(THIRD, SECOND),
        providers: [main(100), second(40), stale(10)],
      });
      assert.include(textOf(result), "runs on codex-second");
      assert.include(
        textOf(result),
        "passed over: codex-third has no current usage reading (stale)",
      );
      assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: SECOND, model: MODEL });
    }),
  );

  it.effect("all accounts full: the task runs as asked, with one note saying why", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: order(MAIN, SECOND, THIRD),
        providers: [main(100), second(100), stale(10)],
      });
      assert.strictEqual(
        textOf(result),
        "Account order: codex is at its limit (Weekly 100% used) and no account in the order can take this task (codex-second is at its limit (Weekly 100% used); codex-third has no current usage reading (stale)), so it runs on codex as asked.",
      );
      assert.strictEqual(dispatched.length, 1);
      assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: MAIN, model: MODEL });
    }),
  );

  it.effect(
    "account reset in the past: its old full value neither moves work off it nor to it",
    () =>
      Effect.gen(function* () {
        // On it: the old 100% is not current, so the task stays (unknown is never a stop).
        const kept = yield* delegate({
          settings: order(MAIN, SECOND),
          providers: [resetPassed(main(0)), second(10)],
        });
        assert.strictEqual(textOf(kept.result), "");
        assert.deepStrictEqual(kept.dispatched[0]?.modelSelection, {
          instanceId: MAIN,
          model: MODEL,
        });
        // As a target: not known until read again, so the next account takes the task.
        const moved = yield* delegate({
          settings: order(SECOND, THIRD),
          providers: [main(100), resetPassed(second(0)), third(10)],
        });
        assert.include(
          textOf(moved.result),
          "codex-second has no current usage reading (resetPassed)",
        );
        assert.deepStrictEqual(moved.dispatched[0]?.modelSelection, {
          instanceId: THIRD,
          model: MODEL,
        });
      }),
  );

  it.effect("an instance the caller named is never moved by the order", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: order(MAIN, SECOND),
        providers: [main(100), second(10)],
        target: { providerInstanceId: MAIN },
      });
      assert.strictEqual(textOf(result), "");
      assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: MAIN, model: MODEL });
    }),
  );

  it.effect("never moves to an account the project turned off", () =>
    Effect.gen(function* () {
      const { result, dispatched } = yield* delegate({
        settings: order(SECOND, THIRD),
        providers: [main(100), second(10), third(10)],
        disabledForProject: [SECOND],
      });
      assert.include(textOf(result), "runs on codex-third");
      assert.include(textOf(result), "codex-second has room, but this project turned it off");
      assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: THIRD, model: MODEL });
    }),
  );

  it.effect("with the pacing gate on too, both notes are in the reply", () =>
    Effect.gen(function* () {
      // The order moves the task to codex-second; the gate then finds it ahead and moves it on.
      const { result, dispatched } = yield* delegate({
        settings: { ...order(SECOND), usagePacingGate: "enforce" },
        providers: [main(100), second(70), third(10)],
      });
      assert.include(textOf(result), "Account order: codex is at its limit");
      assert.include(textOf(result), "Usage pacing: codex-second is ahead of its usage pace");
      assert.deepStrictEqual(dispatched[0]?.modelSelection, { instanceId: THIRD, model: MODEL });
    }),
  );
});
