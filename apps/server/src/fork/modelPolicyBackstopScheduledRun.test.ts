/**
 * Fork: an agent's run_scheduled_task_now on a task whose own model is outside the model policy's
 * rows (modelPolicyBackstop.ts). The backstop refuses the run's new thread; the run then records
 * that refusal, which says the task's model is the problem, not the launch error upstream wraps
 * it in (ScheduledTaskService's run error, one seam line).
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import { assert, it } from "@effect/vitest";
import {
  EnvironmentId,
  MODEL_POLICY_START_ROWS,
  type OrchestrationV2ServerCommand,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ScheduledTaskId,
  type ServerProvider,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as ThreadLaunchService from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ManagedProjectFolders from "../project/ManagedProjectFolders.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ScheduledTaskService from "../scheduledTasks/ScheduledTaskService.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import * as SecretRequests from "../secrets/SecretRequests.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as ModelPolicyBackstop from "./modelPolicyBackstop.ts";

const INSTANCE = ProviderInstanceId.make("claudeAgent");
const HAIKU = "claude-haiku-5-5";
const PROJECT = ProjectId.make("project:scheduled");

const settingsLayer = ServerSettings.layerTest({
  providerInstances: { [INSTANCE]: { driver: "claudeAgent", enabled: true } },
  modelPolicy: { mode: "enforce", allow: MODEL_POLICY_START_ROWS },
} as never);

const registryLayer = Layer.mock(ProviderRegistry.ProviderRegistry)({
  getProviders: Effect.succeed([
    {
      instanceId: INSTANCE,
      driver: ProviderDriverKind.make("claudeAgent"),
      models: [{ slug: HAIKU }],
    } as unknown as ServerProvider,
  ]),
});

const agentScope: McpInvocationContext.McpInvocationScope = {
  environmentId: EnvironmentId.make("environment-here"),
  requestNamespace: "scheduled",
  issuedAt: 0,
  capabilities: new Set(["orchestration" as const]),
  thread: {
    threadId: ThreadId.make("thread:caller"),
    providerSessionId: "session",
    providerInstanceId: INSTANCE,
  },
  client: undefined,
};

it.effect("an agent's run now of a task on an outside model records the policy's refusal", () =>
  Effect.gen(function* () {
    const settings = yield* ServerSettings.ServerSettingsService;
    // The launch dispatches the run's new thread through the backstop, as ThreadManagementService
    // does, and fails the way upstream's launch does: the refusal wrapped in a ThreadLaunchError.
    const launch: ThreadLaunchService.ThreadLaunchService["Service"]["launch"] = (input) =>
      ModelPolicyBackstop.check(
        {
          type: "thread.create",
          commandId: input.commandId,
          threadId: ThreadId.make("thread:run"),
          projectId: input.projectId,
          modelSelection: input.modelSelection,
        } as unknown as OrchestrationV2ServerCommand,
        { getThreadRecords: () => Effect.die("no fork here") },
        { getByCommandId: () => Effect.succeedNone },
        settings,
      ).pipe(
        Effect.mapError(
          (cause) =>
            new ThreadLaunchService.ThreadLaunchError({
              operation: "create-thread",
              commandId: input.commandId,
              projectId: input.projectId,
              cause,
            }),
        ),
        Effect.andThen(Effect.die("the refused run started a thread")),
      ) as never;
    const layer = ScheduledTaskService.layer.pipe(
      Layer.provide(
        Layer.mergeAll(
          ManagedPolicy.layerTest(),
          Layer.mock(ThreadLaunchService.ThreadLaunchService)({ launch }),
          Layer.mock(ThreadManagementService.ThreadManagementService)({}),
          Layer.mock(SecretRequests.SecretRequests)({}),
          Layer.mock(ManagedProjectFolders.ManagedProjectFolders)({
            namedProjectsRoot: "/projects",
            isScratchProject: () => Effect.succeed(false),
          }),
          NodeCrypto.layer,
          Layer.succeed(Scheduler.Scheduler, { register: () => Effect.void }),
        ),
      ),
    );
    yield* Effect.gen(function* () {
      const service = yield* ScheduledTaskService.ScheduledTaskService;
      const { task } = yield* service.upsert({
        id: ScheduledTaskId.make("task:outside-model"),
        title: "On an outside model",
        prompt: "Check progress",
        enabled: true,
        schedule: { type: "interval", everyMs: 60_000 },
        projectId: PROJECT,
        workspaceStrategy: { type: "root" },
        modelSelection: { instanceId: INSTANCE, model: HAIKU },
        runtimeMode: "full-access",
        interactionMode: "default",
      });
      const result = yield* service
        .runNow({ id: task.id })
        .pipe(Effect.provideService(McpInvocationContext.McpInvocationContext, agentScope));
      assert.equal(result.task.lastRunStatus, "failed");
      const error = result.task.lastRunError ?? "";
      assert.include(error, "Model policy refused this pick");
      assert.include(error, HAIKU);
      assert.include(error, "the scheduled task's own model");
      assert.include(error, "schedule_task");
      assert.notInclude(error, "failed during create-thread");
    }).pipe(Effect.provide(layer));
  }).pipe(
    Effect.provide(Layer.mergeAll(settingsLayer, registryLayer, SqlitePersistence.layerMemory)),
  ),
);

it("an error the backstop did not make has no refusal text, wrapped or not", () => {
  assert.isUndefined(ModelPolicyBackstop.refusalText(new Error("Model policy refused this pick")));
  assert.isUndefined(ModelPolicyBackstop.refusalText({ cause: { cause: "x" } }));
  assert.isUndefined(ModelPolicyBackstop.refusalText(undefined));
});
