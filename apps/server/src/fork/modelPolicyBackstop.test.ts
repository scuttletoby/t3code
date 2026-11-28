/**
 * Fork: the model policy's backstop (`modelPolicyBackstop.ts`): which dispatched commands
 * it reads, whose call they come from, and that a pick its route checked is not read again.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  CommandId,
  EnvironmentId,
  MessageId,
  MODEL_POLICY_START_ROWS,
  type ModelPolicyRow,
  type ModelSelection,
  type OrchestrationV2ServerCommand,
  type OrchestrationV2ThreadProjection,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
  type ServerSettingsError,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Schema from "effect/Schema";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProviderSessionManager from "../orchestration-v2/ProviderSessionManager.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as ModelPolicyBackstop from "./modelPolicyBackstop.ts";
import * as ModelPolicyCheck from "./modelPolicyCheck.ts";

const OPUS = "claude-opus-5-5";
const HAIKU = "claude-haiku-5-5";
const INSTANCE = ProviderInstanceId.make("claudeAgent");
const SOURCE = ThreadId.make("thread:source");

const selection = (model: string, effort = "high"): ModelSelection =>
  ({ instanceId: INSTANCE, model, options: [{ id: "effort", value: effort }] }) as ModelSelection;

const create = (modelSelection: ModelSelection) =>
  ({
    type: "thread.create",
    commandId: CommandId.make("command:create"),
    threadId: ThreadId.make("thread:new"),
    modelSelection,
  }) as unknown as OrchestrationV2ServerCommand;
const fork = {
  type: "thread.fork",
  commandId: CommandId.make("command:fork"),
  sourceThreadId: SOURCE,
  targetThreadId: ThreadId.make("thread:fork"),
} as unknown as OrchestrationV2ServerCommand;
const message = (modelSelection?: ModelSelection) =>
  ({
    type: "message.dispatch",
    commandId: CommandId.make("command:message"),
    threadId: SOURCE,
    ...(modelSelection === undefined ? {} : { modelSelection }),
  }) as unknown as OrchestrationV2ServerCommand;

/** The orchestrator's read of the fork's source thread, on the model given. */
const orchestrator = (sourceModel: ModelSelection) => ({
  getThreadRecords: () => Effect.succeed({ thread: { modelSelection: sourceModel } } as never),
});

type Caller = "agent" | "owner" | "linked agent" | "linked owner";
const scopeOf = (caller: Caller): McpInvocationContext.McpInvocationScope => ({
  environmentId: EnvironmentId.make("environment-here"),
  requestNamespace: "backstop",
  issuedAt: 0,
  capabilities: new Set(["orchestration" as const]),
  thread:
    caller === "agent"
      ? {
          threadId: ThreadId.make("thread:caller"),
          providerSessionId: "session",
          providerInstanceId: INSTANCE,
        }
      : undefined,
  client:
    caller === "agent"
      ? undefined
      : caller === "owner"
        ? { sessionId: "session-1", label: "Owner", access: "full-access" }
        : {
            sessionId: "link-1",
            label: "Laptop",
            access: "full-access",
            linked: true,
            ...(caller === "linked agent"
              ? {
                  senderThread: {
                    environmentId: "environment-there",
                    threadId: "thread:s",
                    title: "Sender",
                  } as never,
                }
              : {}),
          },
});

const settingsLayer = (
  mode: "off" | "advise" | "enforce",
  allow: ReadonlyArray<ModelPolicyRow> = MODEL_POLICY_START_ROWS,
) =>
  ServerSettings.layerTest({
    providerInstances: { [INSTANCE]: { driver: "claudeAgent", enabled: true } },
    modelPolicy: { mode, allow },
  } as never);

const registryLayer = Layer.mock(ProviderRegistry.ProviderRegistry)({
  getProviders: Effect.succeed([
    {
      instanceId: INSTANCE,
      driver: ProviderDriverKind.make("claudeAgent"),
      models: [{ slug: OPUS }, { slug: HAIKU }],
    } as unknown as ServerProvider,
  ]),
});

const noReceipts = { getByCommandId: () => Effect.succeedNone };

/** The backstop on one command: `undefined` when it passes, else the refusal's text. */
const run = (
  command: OrchestrationV2ServerCommand,
  options: {
    readonly mode?: "off" | "advise" | "enforce";
    readonly allow?: ReadonlyArray<ModelPolicyRow>;
    /** Undefined: dispatched outside any MCP call (the server's own work). */
    readonly scope?: McpInvocationContext.McpInvocationScope | undefined;
    readonly source?: ModelSelection;
    readonly orchestrator?: Pick<Orchestrator.OrchestratorV2["Service"], "getThreadRecords">;
    readonly receipts?: Pick<
      CommandReceiptStore.CommandReceiptStoreV2["Service"],
      "getByCommandId"
    >;
  } = {},
) =>
  Effect.gen(function* () {
    const settings = yield* ServerSettings.ServerSettingsService;
    const checked = ModelPolicyBackstop.check(
      command,
      options.orchestrator ?? orchestrator(options.source ?? selection(OPUS)),
      options.receipts ?? noReceipts,
      settings,
    );
    return yield* (
      options.scope === undefined
        ? checked
        : Effect.provideService(checked, McpInvocationContext.McpInvocationContext, options.scope)
    ).pipe(
      Effect.as(undefined),
      Effect.catchTags({
        OrchestratorCommandRejectedError: (error) => Effect.succeed(String(error.cause)),
      }),
    );
  }).pipe(
    Effect.provide(
      Layer.mergeAll(settingsLayer(options.mode ?? "enforce", options.allow), registryLayer),
    ),
  );

/** An orchestrator whose thread reads fail: a read where none is due fails the test. */
const unreadable = {
  getThreadRecords: () =>
    Effect.fail(
      new Orchestrator.OrchestratorProjectionError({ threadId: SOURCE, cause: "not readable" }),
    ),
};

describe("the backstop on an agent's command (#876 M4)", () => {
  it.effect("a new thread: an outside model is refused with the rows, an allowed one runs", () =>
    Effect.gen(function* () {
      const refused = yield* run(create(selection(HAIKU)), { scope: scopeOf("agent") });
      assert.include(refused, "Model policy refused this pick");
      assert.include(refused, HAIKU);
      assert.include(refused, OPUS);
      assert.isUndefined(yield* run(create(selection(OPUS)), { scope: scopeOf("agent") }));
      const offEffort = yield* run(create(selection(OPUS, "max")), { scope: scopeOf("agent") });
      assert.include(offEffort, "Model policy refused this pick");
    }),
  );

  it.effect("a thread fork reads its source thread's model: outside refused, inside runs", () =>
    Effect.gen(function* () {
      const refused = yield* run(fork, { scope: scopeOf("agent"), source: selection(HAIKU) });
      assert.include(refused, "source thread's model");
      assert.isUndefined(yield* run(fork, { scope: scopeOf("agent"), source: selection(OPUS) }));
    }),
  );

  it.effect("a message is read only when it names its own model", () =>
    Effect.gen(function* () {
      assert.isUndefined(yield* run(message(), { scope: scopeOf("agent") }));
      assert.include(
        yield* run(message(selection(HAIKU)), { scope: scopeOf("agent") }),
        "Model policy refused this pick",
      );
      assert.isUndefined(yield* run(message(selection(OPUS)), { scope: scopeOf("agent") }));
    }),
  );

  it.effect("a linked machine's agent is read; the owner, here or linked, is not", () =>
    Effect.gen(function* () {
      const outside = create(selection(HAIKU));
      assert.include(
        yield* run(outside, { scope: scopeOf("linked agent") }),
        "Model policy refused",
      );
      assert.isUndefined(yield* run(outside, { scope: scopeOf("owner") }));
      assert.isUndefined(yield* run(outside, { scope: scopeOf("linked owner") }));
    }),
  );

  it.effect("work the server starts outside an MCP call (a scheduled run) is not read", () =>
    Effect.gen(function* () {
      assert.isUndefined(yield* run(create(selection(HAIKU))));
    }),
  );

  it.effect("advise and off let an outside pick run", () =>
    Effect.gen(function* () {
      const outside = create(selection(HAIKU));
      assert.isUndefined(yield* run(outside, { scope: scopeOf("agent"), mode: "advise" }));
      assert.isUndefined(yield* run(outside, { scope: scopeOf("agent"), mode: "off" }));
    }),
  );

  it.effect("a call whose route checked its picks is not read again; the next call is", () =>
    Effect.gen(function* () {
      const scope = scopeOf("agent");
      const routeCheck = yield* ModelPolicyCheck.makePickCheck.pipe(
        Effect.provide(settingsLayer("advise")),
      );
      // The route's own check (say t3_thread_launch) in advise: the outside pick runs, logged once.
      yield* routeCheck(
        "t3_thread_launch",
        Effect.succeed(ModelPolicyCheck.pickOfSelection([], selection(HAIKU), true)),
      ).pipe(Effect.provideService(McpInvocationContext.McpInvocationContext, scope));
      assert.isUndefined(yield* run(create(selection(HAIKU)), { scope }));
      assert.include(
        yield* run(create(selection(HAIKU)), { scope: scopeOf("agent") }),
        "Model policy refused",
      );
    }),
  );

  it.effect("the backstop's own check does not mark the call: its next command is read", () =>
    Effect.gen(function* () {
      const scope = scopeOf("agent");
      assert.isUndefined(yield* run(create(selection(OPUS)), { scope }));
      assert.include(yield* run(fork, { scope, source: selection(HAIKU) }), "Model policy refused");
    }),
  );
});

describe("the backstop's fix round 1 (#876 M4)", () => {
  it.effect("a queued run's edit is read when it names a model, like a message", () =>
    Effect.gen(function* () {
      const edit = (modelSelection?: ModelSelection) =>
        ({
          type: "queued-run.edit",
          commandId: CommandId.make("command:edit"),
          threadId: SOURCE,
          runId: "run:queued",
          text: "edited",
          ...(modelSelection === undefined ? {} : { modelSelection }),
        }) as unknown as OrchestrationV2ServerCommand;
      assert.include(
        yield* run(edit(selection(HAIKU)), { scope: scopeOf("agent") }),
        "Model policy refused this pick",
      );
      assert.isUndefined(yield* run(edit(selection(OPUS)), { scope: scopeOf("agent") }));
      assert.isUndefined(yield* run(edit(), { scope: scopeOf("agent") }));
    }),
  );

  it.effect("a task sent to a linked machine is read as a pick sent there (driver unknown)", () =>
    Effect.gen(function* () {
      const remote = (modelSelection: ModelSelection) =>
        ({
          type: "delegated_task.remote.request",
          commandId: CommandId.make("command:remote"),
          parentThreadId: ThreadId.make("thread:caller"),
          driver: "claudeAgent",
          task: "work",
          modelSelection: { ...modelSelection, instanceId: "instance-there" },
        }) as unknown as OrchestrationV2ServerCommand;
      assert.include(
        yield* run(remote(selection(HAIKU)), { scope: scopeOf("agent") }),
        "Model policy refused this pick",
      );
      // The machine there may turn ultracode on by default: the pick names it off, as it must.
      const opusThere = {
        ...selection(OPUS),
        options: [
          { id: "effort", value: "high" },
          { id: "ultracode", value: false },
        ],
      } as ModelSelection;
      assert.isUndefined(yield* run(remote(opusThere), { scope: scopeOf("agent") }));
    }),
  );

  it("every command type that carries a model is read", () => {
    assert.deepStrictEqual(Object.keys(ModelPolicyBackstop.MODEL_COMMANDS).toSorted(), [
      "delegated_task.remote.request",
      "delegated_task.request",
      "message.dispatch",
      "provider.switch",
      "queued-run.edit",
      "thread.create",
      "thread.model-selection.set",
    ]);
  });

  it.effect("a fork with the policy off reads nothing, and a read that fails fails nothing", () =>
    Effect.gen(function* () {
      const scope = scopeOf("agent");
      assert.isUndefined(yield* run(fork, { scope, mode: "off", orchestrator: unreadable }));
      // With a policy on, a source that cannot be read fails the fork with that read's error.
      const failed = yield* Effect.flip(
        Effect.gen(function* () {
          const settings = yield* ServerSettings.ServerSettingsService;
          yield* ModelPolicyBackstop.check(fork, unreadable, noReceipts, settings).pipe(
            Effect.provideService(McpInvocationContext.McpInvocationContext, scope),
          );
        }).pipe(Effect.provide(Layer.mergeAll(settingsLayer("enforce"), registryLayer))),
      );
      assert.strictEqual(failed._tag, "OrchestratorProjectionError");
    }),
  );

  it.effect("a replay of a command already handled is not read again, nor logged", () =>
    Effect.gen(function* () {
      const logs: Array<Logger.Options<unknown>> = [];
      const handled = {
        getByCommandId: () => Effect.succeedSome({ status: "accepted" } as never),
      };
      const outside = create(selection(HAIKU));
      assert.isUndefined(
        yield* run(outside, { scope: scopeOf("agent"), receipts: handled }).pipe(
          Effect.provide(
            Logger.layer([Logger.make((entry) => logs.push(entry))], { mergeWithExisting: false }),
          ),
        ),
      );
      assert.isFalse(logs.some((entry) => String(entry.message).includes("model policy pick")));
      // A receipt read that fails is no receipt: the command is checked.
      const broken = {
        getByCommandId: () =>
          Effect.fail(
            new CommandReceiptStore.CommandReceiptStoreReadError({
              commandId: "command:create",
              cause: "down",
            } as never),
          ),
      };
      assert.include(
        yield* run(outside, { scope: scopeOf("agent"), receipts: broken as never }),
        "Model policy refused this pick",
      );
    }),
  );

  it.effect("a refusal with many rows fits the 1000 characters a reply keeps, fix included", () =>
    Effect.gen(function* () {
      const rows: Array<ModelPolicyRow> = Array.from({ length: 40 }, (_, index) => ({
        driver: "claudeAgent",
        model: `claude-model-with-a-long-name-${index}`,
        efforts: ["low", "medium", "high"],
        useFor: "work that needs this one model, written out at some length",
      })) as never;
      const refused = (yield* run(create(selection(HAIKU)), {
        scope: scopeOf("agent"),
        allow: [{ driver: "claudeAgent", model: OPUS, efforts: ["high"] } as never, ...rows],
      })) as string;
      assert.isAtMost(Array.from(refused).length, ModelPolicyBackstop.REFUSAL_LIMIT);
      assert.include(refused, OPUS);
      assert.include(refused, "more (all in Settings → Fork → Models agents may use)");
      assert.isTrue(refused.endsWith("and a model from the rows instead)."), refused);
    }),
  );
});

const PROJECT = ProjectId.make("project:backstop");

/** ThreadManagementService with the backstop's line in dispatch, on the settings given. */
const threadsLayer = (
  settings: Layer.Layer<ServerSettings.ServerSettingsService, ServerSettingsError>,
  dispatched: Array<string>,
) =>
  ThreadManagementService.layer.pipe(
    Layer.provide(Layer.mock(ProviderSessionManager.ProviderSessionManagerV2)({})),
    Layer.provide(
      Layer.mock(Orchestrator.OrchestratorV2)({
        getThreadRecords: () =>
          Effect.succeed({
            thread: {
              id: SOURCE,
              projectId: PROJECT,
              deletedAt: null,
              modelSelection: selection(OPUS),
            },
          } as unknown as OrchestrationV2ThreadProjection),
        dispatch: (command) =>
          Effect.sync(() => {
            dispatched.push(command.type);
            return { sequence: 1, storedEvents: [] } as never;
          }),
      }),
    ),
    // Only the service gets the settings: the caller's context has none.
    Layer.provide(settings),
    Layer.provideMerge(
      CommandReceiptStore.layer.pipe(Layer.provide(SqlitePersistence.layerMemory)),
    ),
  );

const isRejected = Schema.is(Orchestrator.OrchestratorCommandRejectedError);

const agentMessage = (commandId: string, model: string) =>
  ({
    type: "message.dispatch",
    commandId: CommandId.make(commandId),
    threadId: SOURCE,
    messageId: MessageId.make(`message:${commandId}`),
    text: "go on",
    attachments: [],
    dispatchMode: { type: "queue_after_active" },
    createdBy: "agent",
    creationSource: "server",
    modelSelection: selection(model),
  }) as unknown as OrchestrationV2ServerCommand;

describe("the backstop in ThreadManagementService.dispatch (#876 M4)", () => {
  it.effect(
    "refuses an agent's outside pick on the service's own settings, the call's context without them",
    () => {
      const dispatched: Array<string> = [];
      return Effect.gen(function* () {
        const threads = yield* ThreadManagementService.ThreadManagementService;
        const asAgent = Effect.provideService(
          McpInvocationContext.McpInvocationContext,
          scopeOf("agent"),
        );
        const refused = yield* Effect.flip(
          threads.dispatch(agentMessage("command:outside", HAIKU)).pipe(asAgent),
        );
        assert.strictEqual(refused._tag, "OrchestratorCommandRejectedError");
        assert.include(
          isRejected(refused) ? String(refused.cause) : "",
          "Model policy refused this pick",
        );
        assert.deepStrictEqual(dispatched, []);
        yield* threads.dispatch(agentMessage("command:inside", OPUS)).pipe(asAgent);
        assert.deepStrictEqual(dispatched, ["message.dispatch"]);
      }).pipe(Effect.provide(threadsLayer(settingsLayer("enforce"), dispatched)));
    },
  );

  it.effect(
    "a retry of an accepted command is answered from its receipt after the policy tightened",
    () => {
      const dispatched: Array<string> = [];
      return Effect.gen(function* () {
        const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;
        const command = agentMessage("command:accepted-before", HAIKU);
        yield* receipts.insertIfAbsent({
          commandId: command.commandId,
          threadId: SOURCE,
          commandType: "message.dispatch",
          acceptedAt: yield* DateTime.now,
          resultSequence: 1,
          status: "accepted",
          error: null,
        });
        const threads = yield* ThreadManagementService.ThreadManagementService;
        yield* threads
          .dispatch(command)
          .pipe(Effect.provideService(McpInvocationContext.McpInvocationContext, scopeOf("agent")));
        assert.deepStrictEqual(dispatched, ["message.dispatch"]);
      }).pipe(Effect.provide(threadsLayer(settingsLayer("enforce"), dispatched)));
    },
  );
});
