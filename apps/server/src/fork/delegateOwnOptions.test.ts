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
import {
  OPTION_CASE_PROVIDERS,
  OPTION_CASE_ROWS,
  OPTION_CASES,
  type OptionCase,
} from "./modelPolicyOptions.testkit.ts";

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
  /** More settings (the MCP profile keys); the calls are then let fail into `refusals`. */
  readonly settings?: Record<string, unknown>;
  readonly refusals?: Array<string>;
  /** Each call's reply, when it is not refused. */
  readonly replies?: Array<unknown>;
}

/**
 * The settings, with a model policy in `policy`. Its rows allow each setup's model with no effort
 * named (a row without efforts), so the policy's check passes and these tests see only the options
 * rule; `modelPolicyCheck.test.ts` tests the check.
 */
const ALLOW_EACH_MODEL = [OPENCODE, CODEX, ACP, CLAUDE].map((setup) => ({
  driver: setup.driver,
  model: setup.model,
}));
const settingsWith = (policy: Launch["policy"] = "advise", more: Record<string, unknown> = {}) =>
  ServerSettings.layerTest({
    modelPolicy: { mode: policy, allow: ALLOW_EACH_MODEL },
    ...more,
  } as never);

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
      settingsWith(input.policy, input.settings),
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
        const { refusals, replies } = input;
        const answered = call(clientRequestId).pipe(
          Effect.tap((reply) => Effect.sync(() => replies?.push(reply))),
        );
        if (refusals === undefined) yield* answered;
        else
          yield* answered.pipe(
            Effect.catch((failure) =>
              Effect.sync(() => refusals.push(`${failure.code}: ${failure.message}`)),
            ),
          );
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
  /**
   * The target the service hands the remote delegation, for one call. `more`: settings beside the
   * policy's mode (rows of its own); `providers`: what this machine has (none unless named).
   */
  const sentTarget = (
    target: unknown,
    policy?: Launch["policy"],
    more: {
      readonly settings?: Record<string, unknown>;
      readonly providers?: ReadonlyArray<ServerProvider>;
    } = {},
  ) =>
    Effect.gen(function* () {
      const sent: Array<unknown> = [];
      const dependencies = Layer.mergeAll(
        NodeServices.layer,
        NodeCrypto.layer,
        Layer.mock(ThreadManagementService.ThreadManagementService)({}),
        Layer.mock(ProviderRegistry.ProviderRegistry)({
          getProviders: Effect.succeed(more.providers ?? []),
        }),
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
        settingsWith(policy, more.settings),
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

  it.effect("enforce: a remote pick outside the rows, or naming no model, is never sent", () =>
    Effect.gen(function* () {
      // Its row allows big-pickle with no effort; the linked machine's driver is not known here.
      assert.deepStrictEqual(yield* sentTarget({ model: "other-model" }, "enforce"), []);
      assert.deepStrictEqual(yield* sentTarget({ providerInstanceId: "opencode" }, "enforce"), []);
      // With no driver known here, every driver's own effort id is read: OpenCode's variant is an
      // effort, which its row (no efforts) does not allow.
      assert.deepStrictEqual(
        yield* sentTarget({ model: OPENCODE.model, options: { variant: "low" } }, "enforce"),
        [],
      );
      // A driverKind the agent names is its own claim, not read: a claim of a driver whose effort
      // id is another does not hide the variant. The claudeAgent and codex rows for the model match
      // their claims and allow no effort, so reading the claim would read no variant and send it.
      const claimRows = {
        modelPolicy: {
          mode: "enforce",
          allow: [
            ...ALLOW_EACH_MODEL,
            { driver: "claudeAgent", model: OPENCODE.model },
            { driver: "codex", model: OPENCODE.model },
          ],
        },
      };
      for (const driverKind of ["opencode", "claudeAgent", "codex"]) {
        assert.deepStrictEqual(
          yield* sentTarget(
            { driverKind, model: OPENCODE.model, options: { variant: "low" } },
            "enforce",
            { settings: claimRows },
          ),
          [],
          `claimed ${driverKind}`,
        );
      }
      // The rows themselves allow the model with no effort named (and ultracode named off, which a
      // pick sent there must name for a claudeAgent row): sent.
      assert.deepStrictEqual(
        yield* sentTarget(
          { driverKind: "claudeAgent", model: OPENCODE.model, options: { ultracode: false } },
          "enforce",
          { settings: claimRows },
        ),
        [
          {
            environmentId: box,
            driverKind: "claudeAgent",
            model: OPENCODE.model,
            options: [{ id: "ultracode", value: false }],
          },
        ],
      );
      assert.deepStrictEqual(yield* sentTarget({ model: OPENCODE.model }, "enforce"), [
        { environmentId: box, model: OPENCODE.model, options: [] },
      ]);
      // Advise: sent as asked.
      assert.deepStrictEqual(yield* sentTarget({ model: "other-model" }, "advise"), [
        { environmentId: box, model: "other-model", options: [] },
      ]);
    }),
  );

  it.effect("enforce: an ACP model's own reasoning id is read from this machine's catalog", () =>
    Effect.gen(function* () {
      const acp = {
        ...providerOf(ACP),
        models: [
          {
            slug: ACP.model,
            name: ACP.model,
            isCustom: false,
            capabilities: {
              optionDescriptors: [
                { ...select("thought_level", ["low", "high"]), role: "reasoning" },
              ],
            },
          },
        ],
      } as ServerProvider;
      const more = {
        settings: {
          modelPolicy: {
            mode: "enforce",
            allow: [{ driver: ACP.driver, model: ACP.model, efforts: ["high"] }],
          },
        },
        providers: [acp],
      };
      const decoy = { model: ACP.model, options: { effort: "high", thought_level: "low" } };
      // A decoy general id beside the model's own control: read as high + low, not sent. Runs
      // again: still not sent.
      for (const _ of [1, 2]) assert.deepStrictEqual(yield* sentTarget(decoy, "enforce", more), []);
      // Without the model in this catalog, the id its driver reads is not known here: not sent
      // (before, only the fixed ids were read and the agent's own `low` went through).
      assert.deepStrictEqual(yield* sentTarget(decoy, "enforce", { settings: more.settings }), []);
      // The general id only, with the catalog: the agent there never reads it, not sent either.
      assert.deepStrictEqual(
        yield* sentTarget({ model: ACP.model, options: { effort: "high" } }, "enforce", more),
        [],
      );
      assert.deepStrictEqual(
        yield* sentTarget(
          { model: ACP.model, options: { thought_level: "high" } },
          "enforce",
          more,
        ),
        [
          {
            environmentId: box,
            model: ACP.model,
            options: [{ id: "thought_level", value: "high" }],
          },
        ],
      );
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
                // Upstream's launch reads the providers to check an agent's mode pick.
                Layer.mock(ProviderRegistry.ProviderRegistry)({ getProviders: Effect.succeed([]) }),
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

describe("create_threads keeps the caller's MCP profile (#681)", () => {
  const sameProject = {};
  const DOCS = { mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } } };
  const narrowed = (profile: string) => ({ mcpThreadProfiles: { [parentThreadId]: profile } });

  it.effect("a narrowed caller cannot make a top-level thread on all; runs again: refused", () =>
    Effect.gen(function* () {
      for (const profile of ["t3-only", "docs"]) {
        const refusals: Array<string> = [];
        const made = yield* launch("create_threads", {
          setup: CLAUDE,
          target: sameProject,
          settings: { ...DOCS, ...narrowed(profile) },
          refusals,
          times: 2,
        });
        assert.strictEqual(made.length, 0, "no thread was made");
        assert.strictEqual(refusals.length, 2);
        for (const refused of refusals) {
          assert.include(refused, `capability_denied: This thread's MCP profile is "${profile}"`);
        }
      }
    }),
  );

  it.effect("stopped half-way: an accepted call's retry is left as it resolves", () =>
    Effect.gen(function* () {
      // Its command is stored (the receipt), so it makes no new thread and is not checked again.
      const refusals: Array<string> = [];
      yield* launch("create_threads", {
        setup: CLAUDE,
        target: sameProject,
        settings: narrowed("t3-only"),
        refusals,
        retryOfAccepted: true,
      });
      assert.deepStrictEqual(refusals, []);
    }),
  );

  it.effect("a caller on all, or a project within the caller's profile, makes its thread", () =>
    Effect.gen(function* () {
      const wide = yield* launch("create_threads", { setup: CLAUDE, target: sameProject });
      assert.strictEqual(wide.length, 1);
      const refusals: Array<string> = [];
      const within = yield* launch("create_threads", {
        setup: CLAUDE,
        target: sameProject,
        settings: {
          ...DOCS,
          ...narrowed("docs"),
          projectSettingsFolded: true,
          projectSettingsOverrides: { [projectId]: { mcpProfile: "t3-only" } },
        },
        refusals,
      });
      assert.deepStrictEqual(refusals, []);
      assert.strictEqual(within.length, 1);
    }),
  );

  it.effect("a project whose profile cannot be worked out is refused, never taken as all", () =>
    Effect.gen(function* () {
      const refusals: Array<string> = [];
      const made = yield* launch("create_threads", {
        setup: CLAUDE,
        target: sameProject,
        settings: {
          ...narrowed("t3-only"),
          projectSettingsFolded: true,
          projectSettingsOverrides: { [projectId]: { mcpProfile: "deleted-profile" } },
        },
        refusals,
      });
      assert.strictEqual(made.length, 0);
      assert.lengthOf(refusals, 1);
      assert.include(
        refusals[0],
        "invalid_request: The new thread's MCP profile cannot be worked out",
      );
    }),
  );
});

describe("the model policy's check on delegate_task and create_threads (#731)", () => {
  /** The rows for these tests: the Claude setup's model at effort high only. */
  const rows = (allow: ReadonlyArray<unknown> = [ROW]) => ({
    modelPolicy: { mode: "enforce", allow },
  });
  const ROW = {
    driver: "claudeAgent",
    model: CLAUDE.model,
    efforts: ["high"],
    defaultEffort: "high",
    useFor: "the work in these tests",
  };
  const named = (effort: string) => ({ model: CLAUDE.model, options: { effort } });
  /** Each tool's dispatches and refusals for one call (or `times` calls). */
  const tryBoth = (input: Omit<Launch, "setup" | "refusals">) =>
    Effect.forEach(TOOLS, (tool) =>
      Effect.gen(function* () {
        const refusals: Array<string> = [];
        const replies: Array<unknown> = [];
        const made = yield* launch(tool, { setup: CLAUDE, ...input, refusals, replies });
        return { tool, made, refusals, replies };
      }),
    );

  it.effect("enforce: an allowed pick runs, with its row's note in delegate_task's reply", () =>
    Effect.gen(function* () {
      for (const { tool, made, refusals, replies } of yield* tryBoth({
        settings: rows(),
        target: named("high"),
      })) {
        assert.deepStrictEqual(refusals, [], tool);
        assert.deepStrictEqual(made, [
          {
            instanceId: instanceOf(CLAUDE),
            model: CLAUDE.model,
            options: [{ id: "effort", value: "high" }],
          },
        ]);
        if (tool === "delegate_task") {
          assert.include(
            (replies[0] as { readonly usagePacingNote?: string }).usagePacingNote,
            `Model policy: ${CLAUDE.model} is for the work in these tests.`,
          );
        }
      }
    }),
  );

  it.effect(
    "enforce: no model (#128), a model in no row (#127), an effort off the row: refused",
    () =>
      Effect.gen(function* () {
        const cases = [
          [{}, "the call names no model"],
          [{ providerInstanceId: CLAUDE.driver }, "the call names no model"],
          [{ model: "other-model" }, "other-model (claudeAgent) is in no row"],
          [named("xhigh"), `${CLAUDE.model} does not allow effort xhigh`],
          [
            { model: CLAUDE.model },
            `${CLAUDE.model} needs an effort from its row, and the pick names none`,
          ],
          [named("ultracode"), `${CLAUDE.model} does not allow effort ultracode`],
        ] as const;
        for (const [target, why] of cases) {
          // Runs again: the same call twice is refused twice, and nothing is dispatched.
          for (const { tool, made, refusals } of yield* tryBoth({
            settings: rows(),
            target,
            times: 2,
          })) {
            assert.deepStrictEqual(made, [], `${tool}: ${why}`);
            assert.lengthOf(refusals, 2, `${tool}: ${why}`);
            for (const refused of refusals) {
              assert.include(refused, `model_unavailable: Model policy refused this pick: ${why}.`);
              assert.include(
                refused,
                `Allowed: claudeAgent ${CLAUDE.model} (efforts high; default high).`,
              );
            }
          }
        }
      }),
  );

  it.effect("empty state: enforce with no rows refuses every pick", () =>
    Effect.gen(function* () {
      for (const { made, refusals } of yield* tryBoth({
        settings: rows([]),
        target: named("high"),
      })) {
        assert.deepStrictEqual(made, []);
        assert.include(refusals[0], "Allowed: none: the policy has no rows");
      }
    }),
  );

  it.effect("advise: a pick outside runs as asked, and delegate_task's reply says so", () =>
    Effect.gen(function* () {
      for (const { tool, made, refusals, replies } of yield* tryBoth({
        settings: { modelPolicy: { mode: "advise", allow: [ROW] } },
        target: { model: "other-model" },
      })) {
        assert.deepStrictEqual(refusals, []);
        assert.lengthOf(made, 1);
        if (tool === "delegate_task") {
          assert.include(
            (replies[0] as { readonly usagePacingNote?: string }).usagePacingNote,
            "Model policy (advise): this pick is outside the policy (other-model (claudeAgent) is in no row); it runs",
          );
        }
      }
    }),
  );

  it.effect("old or missing state: a file without the key, or mode off, checks nothing", () =>
    Effect.gen(function* () {
      for (const settings of [{ modelPolicy: { mode: "off", allow: [] } }, { modelPolicy: {} }]) {
        for (const { made, refusals } of yield* tryBoth({ settings, target: {} })) {
          assert.deepStrictEqual(refusals, []);
          assert.lengthOf(made, 1);
        }
      }
    }),
  );

  it.effect("stopped half-way: the call made again is checked again and runs once", () =>
    Effect.gen(function* () {
      for (const { made, refusals } of yield* tryBoth({
        settings: rows(),
        target: named("high"),
        firstDispatchFails: true,
      })) {
        assert.deepStrictEqual(refusals, []);
        assert.lengthOf(made, 1);
      }
    }),
  );

  it.effect("runs again: the retry of an accepted call starts nothing new and is not checked", () =>
    Effect.gen(function* () {
      // The policy changed since the call was accepted: its retry is left as it resolves.
      for (const { made, refusals } of yield* tryBoth({
        settings: rows([]),
        target: { model: "other-model" },
        retryOfAccepted: true,
      })) {
        assert.deepStrictEqual(refusals, []);
        assert.lengthOf(made, 1);
      }
    }),
  );
});

describe("delegate_task and create_threads: an option the policy cannot read (#793 review)", () => {
  const settings = (mode: string) => ({ modelPolicy: { mode, allow: OPTION_CASE_ROWS } });
  /** The case's provider as one setup, its parent running the allowed effort. */
  const setupOf = (testCase: OptionCase): Setup => {
    const provider = OPTION_CASE_PROVIDERS.find(
      (candidate) => candidate.instanceId === testCase.instanceId,
    )!;
    return {
      name: testCase.name,
      driver: provider.driver,
      model: testCase.model,
      descriptors: provider.models[0]!.capabilities!.optionDescriptors!,
      parent: [testCase.allowed],
    };
  };
  const targetOf = (testCase: OptionCase, withExtra: boolean) => ({
    model: testCase.model,
    options: Object.fromEntries(
      (withExtra ? [testCase.allowed, testCase.extra] : [testCase.allowed]).map((option) => [
        option.id,
        option.value,
      ]),
    ),
  });

  it.effect("local, enforce and advise: refused, nothing dispatched; without it, it runs", () =>
    Effect.gen(function* () {
      for (const testCase of OPTION_CASES) {
        for (const tool of TOOLS) {
          // Here the instance is known: its unknown option was already refused before the policy
          // (upstream's option check), in every mode; enforce's own refusal is behind it.
          for (const mode of ["enforce", "advise"]) {
            const name = `${tool} ${mode}: ${testCase.name}`;
            const refusals: Array<string> = [];
            // Runs again: refused each time, nothing dispatched.
            const made = yield* launch(tool, {
              setup: setupOf(testCase),
              target: targetOf(testCase, true),
              settings: settings(mode),
              times: 2,
              refusals,
            });
            assert.deepStrictEqual(made, [], name);
            assert.lengthOf(refusals, 2, name);
            for (const refused of refusals) {
              assert.include(refused, `Unknown option ${testCase.extra.id}`, name);
            }
          }
          const made = yield* launch(tool, {
            setup: setupOf(testCase),
            target: targetOf(testCase, false),
            settings: settings("enforce"),
          });
          assert.lengthOf(made, 1, `${tool}: ${testCase.name}`);
        }
      }
    }),
  );

  /** The target the service hands the remote delegation (as in the remote tests above). */
  const sentTo = (target: unknown, mode: string) =>
    Effect.gen(function* () {
      const sent: Array<unknown> = [];
      const dependencies = Layer.mergeAll(
        NodeServices.layer,
        NodeCrypto.layer,
        Layer.mock(ThreadManagementService.ThreadManagementService)({}),
        Layer.mock(ProviderRegistry.ProviderRegistry)({
          getProviders: Effect.succeed(OPTION_CASE_PROVIDERS),
        }),
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
        ServerSettings.layerTest(settings(mode) as never),
      );
      const refused = yield* Effect.gen(function* () {
        const service = yield* OrchestratorMcpService.OrchestratorMcpService;
        return yield* service
          .delegateTask(scopeOf(OPENCODE), {
            task: "Review the change on the box.",
            target: decodeTarget({ environmentId: box, ...(target as object) }),
            mode: "async",
            clientRequestId: "option-case-remote",
          })
          .pipe(
            Effect.flip,
            Effect.map((failure) =>
              typeof failure === "string" ? undefined : (failure as OrchestratorMcpFailure).message,
            ),
          );
      }).pipe(Effect.provide(OrchestratorMcpService.layer.pipe(Layer.provide(dependencies))));
      return { sent, refused };
    });

  it.effect("sent to a linked machine: enforce refuses it before it is sent; advise sends it", () =>
    Effect.gen(function* () {
      for (const testCase of OPTION_CASES) {
        // Runs again: refused each time, nothing sent.
        for (const _ of [1, 2]) {
          const { sent, refused } = yield* sentTo(targetOf(testCase, true), "enforce");
          assert.deepStrictEqual(sent, [], testCase.name);
          assert.include(refused, `Model policy refused this pick: ${testCase.why}`, testCase.name);
          assert.include(refused, testCase.fix, testCase.name);
          assert.include(refused, "target.", testCase.name);
        }
        assert.lengthOf((yield* sentTo(targetOf(testCase, false), "enforce")).sent, 1);
        assert.lengthOf((yield* sentTo(targetOf(testCase, true), "advise")).sent, 1);
      }
    }),
  );
});
