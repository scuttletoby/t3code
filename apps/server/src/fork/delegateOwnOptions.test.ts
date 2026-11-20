/**
 * Fork: a child's own options on every launch path (`ownOptions` in `delegateChecks.ts`). While a
 * model policy is on, a call that names its model keeps only the options it names; a call that
 * names no model, a retry of an accepted call, and every call with the policy off are as upstream.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  type EnvironmentId,
  NodeId,
  type OrchestratorMcpFailure,
  OrchestratorMcpTarget,
  type OrchestrationV2ThreadProjection,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  type ProviderOptionDescriptor,
  type ProviderOptionSelection,
  RunId,
  ServerSettings as ServerSettingsSchema,
  type ServerProvider,
  ThreadId,
} from "@t3tools/contracts";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as Context from "effect/Context";
import * as SecureRandom from "effect/crypto/SecureRandom";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";
import { HttpClient } from "effect/http";

import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import type { McpInvocationScope } from "../mcp/McpInvocationContext.ts";
import * as OrchestratorMcpService from "../mcp/OrchestratorMcpService.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ProviderAdapterRegistry from "../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ThreadLaunchService from "../orchestration-v2/ThreadLaunchService.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ScheduledTaskService from "../scheduledTasks/ScheduledTaskService.ts";
import * as SecretRequests from "../secrets/SecretRequests.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as GitVcsDriver from "../vcs/GitVcsDriver.ts";
import * as DelegateChecks from "./delegateChecks.ts";

const parentThreadId = ThreadId.make("thread:own-options-parent");
const childThreadId = ThreadId.make("thread:own-options-child");
const taskId = NodeId.make("node:own-options-task");
const projectId = ProjectId.make("project:own-options");
const here = "environment:own-options-here" as EnvironmentId;
const box = "environment:own-options-box" as EnvironmentId;
const decodeTarget = Schema.decodeUnknownSync(OrchestratorMcpTarget);
const defaultSettings = Schema.decodeSync(ServerSettingsSchema)({});

const select = (id: string, values: ReadonlyArray<string>): ProviderOptionDescriptor => ({
  id,
  label: id,
  type: "select",
  options: values.map((value) => ({ id: value, label: value })),
});

/** One provider with one model, its options as the driver advertises them. */
interface Setup {
  readonly name: string;
  readonly driver: string;
  readonly model: string;
  readonly descriptors: ReadonlyArray<ProviderOptionDescriptor>;
  /** What the parent thread runs. */
  readonly parent: ReadonlyArray<ProviderOptionSelection>;
}

const OPENCODE: Setup = {
  name: "OpenCode agent",
  driver: "opencode",
  model: "big-pickle",
  descriptors: [select("variant", ["low", "high"]), select("agent", ["build", "plan"])],
  parent: [
    { id: "variant", value: "high" },
    { id: "agent", value: "plan" },
  ],
};

const CODEX: Setup = {
  name: "Codex service tier and access program",
  driver: "codex",
  model: "gpt-5.4",
  descriptors: [
    select("reasoningEffort", ["low", "high"]),
    select("serviceTier", ["default", "fast"]),
    select("cyberAccessProgram", ["standard", "trusted"]),
  ],
  parent: [
    { id: "reasoningEffort", value: "high" },
    { id: "serviceTier", value: "fast" },
    { id: "cyberAccessProgram", value: "trusted" },
  ],
};

const ACP: Setup = {
  name: "ACP session mode and mode-category option",
  driver: "cursor",
  model: "auto",
  descriptors: [
    select("_t3/session-mode", ["ask", "code"]),
    // An ACP config option of category "mode" keeps its own id (provider-acp sessionConfig.ts).
    select("approval_mode", ["default", "yolo"]),
  ],
  parent: [
    { id: "_t3/session-mode", value: "code" },
    { id: "approval_mode", value: "yolo" },
  ],
};

const CLAUDE: Setup = {
  name: "Claude ultracode",
  driver: "claudeAgent",
  model: "claude-opus-4-7",
  descriptors: [
    select("effort", ["high", "xhigh"]),
    { id: "ultracode", label: "Ultracode", type: "boolean" },
    { id: "fastMode", label: "Fast", type: "boolean" },
  ],
  parent: [
    { id: "effort", value: "high" },
    { id: "fastMode", value: true },
  ],
};

const instanceOf = (setup: Setup) => ProviderInstanceId.make(setup.driver);

const providerOf = (setup: Setup): ServerProvider => ({
  instanceId: instanceOf(setup),
  driver: ProviderDriverKind.make(setup.driver),
  enabled: true,
  installed: true,
  version: "test",
  status: "ready",
  auth: { status: "authenticated" },
  checkedAt: "2026-11-20T12:00:00.000Z",
  models: [
    {
      slug: setup.model,
      name: setup.model,
      isCustom: false,
      capabilities: { optionDescriptors: [...setup.descriptors] },
    },
    // A second model on the same account, which inherits nothing either way.
    { slug: "other-model", name: "other-model", isCustom: false, capabilities: null },
  ],
  slashCommands: [],
  skills: [],
});

const scopeOf = (setup: Setup): McpInvocationScope => ({
  environmentId: here,
  requestNamespace: "provider-session:own-options",
  thread: {
    threadId: parentThreadId,
    providerSessionId: "provider-session:own-options",
    providerInstanceId: instanceOf(setup),
  },
  client: undefined,
  capabilities: new Set(["orchestration"]),
  issuedAt: 1,
});

const parentSelection = (setup: Setup, options = setup.parent) => ({
  instanceId: instanceOf(setup),
  model: setup.model,
  ...(options === undefined ? {} : { options }),
});

const parentProjection = (
  modelSelection: ReturnType<typeof parentSelection>,
  subagents: ReadonlyArray<unknown>,
): OrchestrationV2ThreadProjection =>
  ({
    thread: {
      id: parentThreadId,
      projectId,
      title: "Parent",
      createdBy: "user",
      creationSource: "web",
      modelSelection,
      runtimeMode: "full-access",
      interactionMode: "default",
      lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: parentThreadId },
    },
    runs: [
      {
        id: RunId.make("run:own-options-parent"),
        ordinal: 1,
        status: "running",
        rootNodeId: NodeId.make("node:own-options-root"),
        providerInstanceId: modelSelection.instanceId,
        modelSelection,
      },
    ],
    contextTransfers: [],
    subagents,
  }) as unknown as OrchestrationV2ThreadProjection;

type Tool = "delegate_task" | "create_threads";

interface Launch {
  readonly setup: Setup;
  readonly target: unknown;
  /** The parent's options; `null` for a parent thread that has none (an old one). */
  readonly parentOptions?: ReadonlyArray<ProviderOptionSelection> | null;
  /** The call was accepted before: its receipt is stored. */
  readonly retryOfAccepted?: boolean;
  /** The first dispatch fails, as a call stopped half-way; the same call is then made again. */
  readonly firstDispatchFails?: boolean;
  /** How many times the same service runs the call (each with its own request id). */
  readonly times?: number;
  /** The model policy's mode; `advise` unless named. */
  readonly policy?: "off" | "advise" | "enforce";
}

const settingsWith = (policy: Launch["policy"] = "advise") =>
  ServerSettings.layerTest({ modelPolicy: { mode: policy } });

/** The model selection each dispatch carried, for one tool on one service. */
const launch = (tool: Tool, input: Launch) =>
  Effect.gen(function* () {
    const { setup } = input;
    const inherited = parentSelection(
      setup,
      input.parentOptions === null ? undefined : (input.parentOptions ?? setup.parent),
    );
    const selections: Array<unknown> = [];
    let failNext = input.firstDispatchFails === true;
    const task = {
      id: taskId,
      threadId: parentThreadId,
      origin: "app_owned",
      childThreadId,
      providerInstanceId: inherited.instanceId,
      model: inherited.model,
      status: "running",
      result: null,
    };
    const dependencies = Layer.mergeAll(
      NodeServices.layer,
      NodeCrypto.layer,
      Layer.mock(ThreadManagementService.ThreadManagementService)({
        hasReceipt: () => Effect.succeed(input.retryOfAccepted === true),
        assertProviderInstanceEnabled: () => Effect.void,
        isProviderInstanceEnabledForProject: () => Effect.succeed(true),
        getThreadShell: () => Effect.succeed({ modelSelection: inherited } as never),
        getThreadRecords: (threadId) =>
          Effect.succeed(
            threadId === parentThreadId
              ? parentProjection(inherited, selections.length > 0 ? [task] : [])
              : ({
                  thread: { id: childThreadId, title: "Child", modelSelection: inherited },
                  runs: [],
                  contextTransfers: [],
                  messages: [],
                  subagents: [],
                  providerThreads: [],
                  providerTurns: [],
                  runtimeRequests: [],
                  turnItems: [],
                } as unknown as OrchestrationV2ThreadProjection),
          ),
        dispatch: (command) =>
          Effect.suspend(() => {
            const { modelSelection } = command as { readonly modelSelection?: unknown };
            if (modelSelection === undefined) return Effect.succeed({ sequence: 1 } as never);
            if (failNext) {
              failNext = false;
              return Effect.die("the server stopped before the command was stored");
            }
            selections.push(modelSelection);
            return Effect.succeed({
              sequence: 1,
              storedEvents: [{ event: { type: "subagent.updated", payload: task } }],
            } as never);
          }),
      }),
      Layer.mock(ProviderRegistry.ProviderRegistry)({
        getProviders: Effect.succeed([providerOf(setup)]),
        refreshInstance: () => Effect.succeed([providerOf(setup)]),
      }),
      Layer.succeed(
        ProviderAdapterRegistry.ProviderAdapterRegistryV2,
        ProviderAdapterRegistry.ProviderAdapterRegistryV2.of({
          list: () => Effect.succeed([instanceOf(setup)]),
          get: (instanceId) =>
            Effect.succeed({
              instanceId,
            } as unknown as ProviderAdapter.ProviderAdapterV2["Service"]),
        }),
      ),
      Layer.mock(ProjectService.ProjectService)({ getById: () => Effect.succeedNone }),
      Layer.mock(SecretRequests.SecretRequests)({}),
      Layer.mock(RemoteDelegation.RemoteDelegation)({}),
      Layer.mock(ScheduledTaskService.ScheduledTaskService)({}),
      Layer.mock(ThreadLaunchService.ThreadLaunchService)({}),
      Layer.mock(GitVcsDriver.GitVcsDriver)({}),
      settingsWith(input.policy),
    );
    yield* Effect.gen(function* () {
      const service = yield* OrchestratorMcpService.OrchestratorMcpService;
      const target = decodeTarget(input.target);
      const call = (clientRequestId: string): Effect.Effect<unknown, OrchestratorMcpFailure> =>
        tool === "delegate_task"
          ? service.delegateTask(scopeOf(setup), {
              task: "Review the change.",
              target,
              mode: "async",
              clientRequestId,
            })
          : service.createThreads(scopeOf(setup), { threads: [{ target }], clientRequestId });
      for (let index = 0; index < (input.times ?? 1); index += 1) {
        const clientRequestId = `own-options-${index}`;
        if (input.firstDispatchFails === true && index === 0) {
          const stopped = yield* Effect.exit(call(clientRequestId));
          assert.strictEqual(stopped._tag, "Failure", "the first dispatch stops the call");
        }
        yield* call(clientRequestId);
      }
    }).pipe(Effect.provide(OrchestratorMcpService.layer.pipe(Layer.provide(dependencies))));
    return selections;
  });

const TOOLS: ReadonlyArray<Tool> = ["delegate_task", "create_threads"];
const SETUPS = [OPENCODE, CODEX, ACP, CLAUDE];

/** Each tool's single dispatch, checked against the options the child should run. */
const expectOptions = (
  input: Launch,
  options: ReadonlyArray<ProviderOptionSelection> | undefined,
  model = input.setup.model,
) =>
  Effect.forEach(TOOLS, (tool) =>
    launch(tool, input).pipe(
      Effect.map((selections) =>
        assert.deepStrictEqual(
          selections,
          Array.from({ length: input.times ?? 1 }, () => ({
            instanceId: instanceOf(input.setup),
            model,
            ...(options === undefined ? {} : { options }),
          })),
          `${tool}: ${input.setup.name}: ${JSON.stringify(input.target)}`,
        ),
      ),
    ),
  );

describe("a child's own options, local delegate_task and create_threads", () => {
  it.effect.each(SETUPS)("$name: a call that names its model takes none of the parent's", (setup) =>
    expectOptions({ setup, target: { model: setup.model } }, []),
  );

  it.effect.each(SETUPS)(
    "$name: a call that names no model keeps the parent's (upstream)",
    (setup) =>
      Effect.andThen(
        expectOptions({ setup, target: {} }, setup.parent),
        // Naming the account but not the model is still no model named.
        expectOptions({ setup, target: { providerInstanceId: setup.driver } }, setup.parent),
      ),
  );

  it.effect("the options a call names are kept, whatever their kind", () =>
    Effect.all([
      expectOptions(
        { setup: OPENCODE, target: { model: OPENCODE.model, options: { agent: "build" } } },
        [{ id: "agent", value: "build" }],
      ),
      expectOptions(
        { setup: OPENCODE, target: { model: OPENCODE.model, options: { variant: "low" } } },
        [{ id: "variant", value: "low" }],
      ),
      expectOptions(
        {
          setup: CODEX,
          target: { model: CODEX.model, options: { serviceTier: "fast", reasoningEffort: "low" } },
        },
        [
          { id: "reasoningEffort", value: "low" },
          { id: "serviceTier", value: "fast" },
        ],
      ),
      expectOptions(
        {
          setup: CODEX,
          target: { model: CODEX.model, options: { cyberAccessProgram: "standard" } },
        },
        [{ id: "cyberAccessProgram", value: "standard" }],
      ),
      expectOptions(
        { setup: ACP, target: { model: ACP.model, options: { "_t3/session-mode": "ask" } } },
        [{ id: "_t3/session-mode", value: "ask" }],
      ),
      expectOptions(
        { setup: ACP, target: { model: ACP.model, options: { approval_mode: "default" } } },
        [{ id: "approval_mode", value: "default" }],
      ),
      // The same value as the parent's is still the call's own.
      expectOptions(
        { setup: ACP, target: { model: ACP.model, options: { approval_mode: "yolo" } } },
        [{ id: "approval_mode", value: "yolo" }],
      ),
    ]),
  );

  it.effect("an old effort `ultracode` keeps the ultracode switch it stands for", () =>
    expectOptions(
      { setup: CLAUDE, target: { model: CLAUDE.model, options: { effort: "ultracode" } } },
      [
        { id: "effort", value: "xhigh" },
        { id: "ultracode", value: true },
      ],
    ),
  );

  it.effect("`options: {}` with a model clears them; another model inherits nothing", () =>
    Effect.andThen(
      expectOptions({ setup: CODEX, target: { model: CODEX.model, options: {} } }, []),
      expectOptions({ setup: CODEX, target: { model: "other-model" } }, undefined, "other-model"),
    ),
  );

  it.effect("old or empty state: a parent with no options, or with none of the model's", () =>
    Effect.andThen(
      expectOptions(
        { setup: OPENCODE, parentOptions: null, target: { model: OPENCODE.model } },
        [],
      ),
      // An option the model no longer offers is dropped as upstream drops it, model named or not.
      expectOptions(
        {
          setup: OPENCODE,
          parentOptions: [{ id: "retired", value: "x" }],
          target: { model: OPENCODE.model },
        },
        [],
      ),
    ),
  );

  it.effect("runs again: each call on the same server gets the same options", () =>
    expectOptions({ setup: OPENCODE, times: 3, target: { model: OPENCODE.model } }, []),
  );

  it.effect("stopped half-way: the same call again gets the same options", () =>
    expectOptions(
      {
        setup: CODEX,
        firstDispatchFails: true,
        target: { model: CODEX.model, options: { serviceTier: "fast" } },
      },
      [{ id: "serviceTier", value: "fast" }],
    ),
  );

  it.effect("a retry of an accepted call is left as it resolves (its command is stored)", () =>
    expectOptions(
      { setup: OPENCODE, retryOfAccepted: true, target: { model: OPENCODE.model } },
      OPENCODE.parent,
    ),
  );

  it.effect("enforce is the same rule as advise", () =>
    expectOptions({ setup: CODEX, policy: "enforce", target: { model: CODEX.model } }, []),
  );

  it.effect.each(SETUPS)("$name: with the model policy off, nothing changes (upstream)", (setup) =>
    Effect.andThen(
      expectOptions({ setup, policy: "off", target: { model: setup.model } }, setup.parent),
      expectOptions({ setup, policy: "off", target: {} }, setup.parent),
    ),
  );
});

describe("a child's own options, remote delegate_task", () => {
  /** The target the service hands the remote delegation, for one call. */
  const sentTarget = (target: unknown, policy?: Launch["policy"]) =>
    Effect.gen(function* () {
      const sent: Array<unknown> = [];
      const dependencies = Layer.mergeAll(
        NodeServices.layer,
        NodeCrypto.layer,
        Layer.mock(ThreadManagementService.ThreadManagementService)({}),
        Layer.mock(ProviderRegistry.ProviderRegistry)({}),
        Layer.mock(ProviderAdapterRegistry.ProviderAdapterRegistryV2)({}),
        Layer.mock(ProjectService.ProjectService)({}),
        Layer.mock(SecretRequests.SecretRequests)({}),
        Layer.mock(RemoteDelegation.RemoteDelegation)({
          delegate: (_scope, input) =>
            Effect.sync(() => {
              sent.push(input.target);
            }).pipe(Effect.andThen(Effect.fail("stop after the hand-over" as never))),
        }),
        Layer.mock(ScheduledTaskService.ScheduledTaskService)({}),
        Layer.mock(ThreadLaunchService.ThreadLaunchService)({}),
        Layer.mock(GitVcsDriver.GitVcsDriver)({}),
        settingsWith(policy),
      );
      yield* Effect.gen(function* () {
        const service = yield* OrchestratorMcpService.OrchestratorMcpService;
        yield* Effect.exit(
          service.delegateTask(scopeOf(OPENCODE), {
            task: "Review the change on the box.",
            target: decodeTarget({ environmentId: box, ...(target as object) }),
            mode: "async",
            clientRequestId: "own-options-remote",
          }),
        );
      }).pipe(Effect.provide(OrchestratorMcpService.layer.pipe(Layer.provide(dependencies))));
      return sent;
    });

  it.effect("the service sends a call that names its model only the options it names", () =>
    Effect.gen(function* () {
      assert.deepStrictEqual(yield* sentTarget({ model: OPENCODE.model }), [
        { environmentId: box, model: OPENCODE.model, options: [] },
      ]);
      assert.deepStrictEqual(
        yield* sentTarget({ model: OPENCODE.model, options: { agent: "build" } }),
        [{ environmentId: box, model: OPENCODE.model, options: [{ id: "agent", value: "build" }] }],
      );
      // No model named: the target goes as it came, and the other side inherits as upstream does.
      assert.deepStrictEqual(yield* sentTarget({ providerInstanceId: "opencode" }), [
        { environmentId: box, providerInstanceId: ProviderInstanceId.make("opencode") },
      ]);
      // The policy off: as it came, as before the fork's rule.
      assert.deepStrictEqual(yield* sentTarget({ model: OPENCODE.model }, "off"), [
        { environmentId: box, model: OPENCODE.model },
      ]);
    }),
  );

  /** What the launch on the linked machine carried, through the real remote delegation. */
  const launchedThere = (target: Record<string, unknown>) =>
    Effect.scoped(
      Effect.gen(function* () {
        const launched: Array<{ readonly modelSelection?: unknown }> = [];
        const inherited = parentSelection(OPENCODE);
        const remote = yield* Layer.build(
          RemoteDelegation.layer.pipe(
            Layer.provide(
              Layer.mergeAll(
                Layer.mock(PeerForwarding.PeerForwarding)({
                  call: ((
                    _scope: unknown,
                    tool: { readonly name: string },
                    _env: unknown,
                    params: never,
                  ) =>
                    tool.name === "t3_thread_launch"
                      ? Effect.sync(() => {
                          launched.push(params);
                          return {
                            threadId: ThreadId.make("thread:box-child"),
                            runId: null,
                            modelSelection: inherited,
                          };
                        })
                      : Effect.die(`unexpected call ${tool.name}`)) as never,
                }),
                Layer.mock(ThreadManagementService.ThreadManagementService)({
                  getThreadEventSequence: () => Effect.succeed(0) as never,
                  getThreadRecords: ((
                    _threadId: ThreadId,
                    _parts: unknown,
                    only?: { readonly subagentIds?: ReadonlyArray<NodeId> },
                  ) =>
                    only?.subagentIds !== undefined
                      ? Effect.never
                      : Effect.succeed({
                          ...parentProjection(inherited, []),
                          subagents: [
                            { id: taskId, remoteChild: { threadId: "thread:box-child" } },
                          ],
                        })) as never,
                  dispatch: () =>
                    Effect.succeed({
                      storedEvents: [
                        { event: { type: "subagent.updated", payload: { id: taskId } } },
                      ],
                    } as never),
                }),
                Layer.mock(PeerLinks.PeerLinks)({ get: () => Effect.succeedNone }),
                Layer.mock(ProjectionStore.ProjectionStoreV2)({}),
                Layer.mock(ProjectService.ProjectService)({}),
                Layer.mock(RepositoryIdentityResolver.RepositoryIdentityResolver)({}),
                Layer.mock(ServerEnvironment.ServerEnvironment)({
                  getEnvironmentId: Effect.succeed(here),
                }),
                Layer.mock(ServerSettings.ServerSettingsService)({
                  getSettings: Effect.succeed(defaultSettings),
                }),
                Layer.succeed(SecureRandom.SecureRandom, {
                  uuidV4: Effect.succeed("00000000-0000-4000-8000-000000000000"),
                } as never),
                Layer.succeed(
                  HttpClient.HttpClient,
                  HttpClient.make(() => Effect.die("the box is not asked")),
                ),
              ),
            ),
          ),
        ).pipe(Effect.map(Context.get(RemoteDelegation.RemoteDelegation)));
        // The target the checks send while a model policy is on.
        const sent = DelegateChecks.remoteTarget({
          ...decodeTarget({ ...target, projectId: "project:box" }),
          environmentId: box,
        });
        yield* remote.delegate(scopeOf(OPENCODE) as never, {
          task: "Review the change on the box.",
          target: sent,
          mode: "async",
          clientRequestId: "own-options-box",
        });
        return launched.map((params) => params.modelSelection);
      }),
    );

  it.effect(
    "with a policy on, the launch there takes the parent's agent only if no model is named",
    () =>
      Effect.gen(function* () {
        const instanceId = instanceOf(OPENCODE);
        assert.deepStrictEqual(yield* launchedThere({ model: OPENCODE.model }), [
          { instanceId, model: OPENCODE.model, options: [] },
        ]);
        assert.deepStrictEqual(
          yield* launchedThere({ model: OPENCODE.model, options: { variant: "low" } }),
          [{ instanceId, model: OPENCODE.model, options: [{ id: "variant", value: "low" }] }],
        );
        assert.deepStrictEqual(yield* launchedThere({}), [
          { instanceId, model: OPENCODE.model, options: OPENCODE.parent },
        ]);
      }),
  );
});
