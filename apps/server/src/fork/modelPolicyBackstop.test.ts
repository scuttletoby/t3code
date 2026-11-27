/**
 * Fork: the model policy's backstop (`modelPolicyBackstop.ts`): which dispatched commands
 * it reads, whose call they come from, and that a pick its route checked is not read again.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  CommandId,
  EnvironmentId,
  MODEL_POLICY_START_ROWS,
  type ModelSelection,
  type OrchestrationV2ServerCommand,
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
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

const layer = (mode: "off" | "advise" | "enforce") =>
  Layer.mergeAll(
    ServerSettings.layerTest({ modelPolicy: { mode, allow: MODEL_POLICY_START_ROWS } } as never),
    Layer.mock(ProviderRegistry.ProviderRegistry)({
      getProviders: Effect.succeed([
        {
          instanceId: INSTANCE,
          driver: ProviderDriverKind.make("claudeAgent"),
          models: [{ slug: OPUS }, { slug: HAIKU }],
        } as unknown as ServerProvider,
      ]),
    }),
  );

/** The backstop on one command: `undefined` when it passes, else the refusal's text. */
const run = (
  command: OrchestrationV2ServerCommand,
  options: {
    readonly mode?: "off" | "advise" | "enforce";
    /** Undefined: dispatched outside any MCP call (the server's own work). */
    readonly scope?: McpInvocationContext.McpInvocationScope | undefined;
    readonly source?: ModelSelection;
  } = {},
) => {
  const checked = ModelPolicyBackstop.checkAgentCommand(
    command,
    orchestrator(options.source ?? selection(OPUS)),
  );
  return (
    options.scope === undefined
      ? checked
      : Effect.provideService(checked, McpInvocationContext.McpInvocationContext, options.scope)
  ).pipe(
    Effect.as(undefined),
    Effect.catchTags({
      OrchestratorCommandRejectedError: (error) => Effect.succeed(String(error.cause)),
    }),
    Effect.provide(layer(options.mode ?? "enforce")),
  );
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
        Effect.provide(layer("advise")),
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
