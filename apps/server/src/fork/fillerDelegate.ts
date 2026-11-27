/**
 * Fork: POST/GET /api/fork/delegate start and list a parent thread's children for a program (the slot filler)
 * through this server's own delegate_task; an idle parent gets one wake message first. A session token only, no
 * browser. The same requestId returns the same task, and the wake is sent once per request (docs/fork/README.md).
 */
import {
  AuthOrchestrationOperateScope,
  AuthOrchestrationReadScope,
  CommandId,
  type EnvironmentId,
  MessageId,
  OrchestratorMcpClientRequestId,
  OrchestratorMcpDelegateTaskInput,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import { McpSchema } from "effect/ai";
import { HttpRouter, HttpServerRequest, HttpServerResponse } from "effect/http";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { admitSend } from "./agentSlotRoutes.ts";

export const FILLER_DELEGATE_PATH = "/api/fork/delegate";

/** One namespace per parent thread: the same request id gives the same task after a restart. */
const fillerNamespace = (threadId: string) => `slot_filler:${threadId}`;

/** How long a start waits for an idle parent's run (the wake starts it): 20 tries, 2 s apart. */
export const PARENT_RUN_TRIES = 20;
const WAKE_TEXT_MAX = 600;

/** What the route needs of the MCP server: its own `tools/call`. */
export interface FillerMcp {
  readonly callTool: (request: {
    readonly name: string;
    readonly arguments: Record<string, unknown>;
  }) => Effect.Effect<
    McpSchema.CallToolResult,
    McpSchema.McpErrorBase | McpSchema.InternalError | McpSchema.InvalidParams,
    McpSchema.McpServerClient
  >;
}

let server: FillerMcp | undefined;
let environmentId: EnvironmentId | undefined;

/** Installed by the MCP layer for its life (fork/mcpT3ToolRule.ts), beside lens_start's. */
export const installFillerDelegateServer = (mcp: FillerMcp, here: EnvironmentId | undefined) =>
  Effect.acquireRelease(
    Effect.sync(() => {
      server = mcp;
      environmentId = here;
    }),
    () =>
      Effect.sync(() => {
        if (server === mcp) server = undefined;
      }),
  );

const serverClient = McpSchema.McpServerClient.of({
  clientId: 0,
  protocolVersion: "2025-06-18",
  clientCapabilities: {},
  clientInfo: { name: "t3-slot-filler", version: "1" },
  initializePayload: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "t3-slot-filler", version: "1" },
  },
  getClient: Effect.die("the slot filler's own delegate_task call has no MCP client"),
});

/** The request id: also part of the wake's message and command ids, so a plain id only. */
const RequestId = OrchestratorMcpClientRequestId.check(
  Schema.makeFilter((value) =>
    /^[A-Za-z0-9._:-]{1,128}$/.test(value)
      ? true
      : "requestId: letters, digits and . _ : - only, at most 128",
  ),
);

export const FillerStartInput = Schema.Struct({
  parentThreadId: ThreadId,
  requestId: RequestId,
  /** The message an idle parent gets so it has a run; a default names the task's title. */
  wake: Schema.optional(Schema.String),
  task: OrchestratorMcpDelegateTaskInput,
});
export type FillerStartInput = typeof FillerStartInput.Type;
const decodeStart = Schema.decodeUnknownOption(Schema.fromJsonString(FillerStartInput));
const encodeDelegate = Schema.encodeSync(OrchestratorMcpDelegateTaskInput);

export type StartAnswer =
  | { readonly type: "started"; readonly taskId: string; readonly childThreadId: string | null }
  | { readonly type: "refused"; readonly code: string; readonly message: string };

export interface FillerParent {
  readonly threadId: ThreadId;
  readonly providerInstanceId: ProviderInstanceId;
}

/** What a start reads and does; the route's is below, tests give their own. */
export interface FillerWorld {
  /** The parent, or null when it is gone, deleted or archived. */
  readonly parent: (threadId: ThreadId) => Effect.Effect<FillerParent | null>;
  readonly delegate: (
    parent: FillerParent,
    task: OrchestratorMcpDelegateTaskInput,
  ) => Effect.Effect<StartAnswer>;
  /** Sends the wake once per request id ("sent" also when sent before); "no_slot": not sent. */
  readonly wake: (
    parent: FillerParent,
    requestId: string,
    text: string,
  ) => Effect.Effect<"sent" | "no_slot">;
  readonly pause: Effect.Effect<void>;
}

const refused = (code: string, message: string): StartAnswer => ({
  type: "refused",
  code,
  message,
});

const wakeTextOf = (input: FillerStartInput) =>
  (
    input.wake?.trim() ||
    `[slot filler] Starting "${input.task.title ?? input.requestId}" as your child; no action needed unless it fails.`
  ).slice(0, WAKE_TEXT_MAX);

/** One start: as the parent, waking it once if it has no run. Never two children for one request id. */
export const runFillerStart = Effect.fn("FillerDelegate.start")(function* (
  world: FillerWorld,
  input: FillerStartInput,
) {
  const parent = yield* world.parent(input.parentThreadId);
  if (parent === null) {
    return refused(
      "parent_missing",
      `thread ${input.parentThreadId} is gone, deleted or archived; nothing started`,
    );
  }
  // async only (a wait-mode child would hold this request), under the caller's own request id.
  const task: OrchestratorMcpDelegateTaskInput = {
    ...input.task,
    mode: "async",
    clientRequestId: input.requestId,
  };
  let answer = yield* world.delegate(parent, task);
  if (answer.type === "started" || answer.code !== "parent_not_active") return answer;
  const woke = yield* world.wake(parent, input.requestId, wakeTextOf(input));
  if (woke === "no_slot") {
    return refused(
      "slots_full",
      "the parent has no run and no agent slot is free to wake it; nothing started",
    );
  }
  for (let attempt = 0; attempt < PARENT_RUN_TRIES; attempt += 1) {
    yield* world.pause;
    answer = yield* world.delegate(parent, task);
    if (answer.type === "started" || answer.code !== "parent_not_active") return answer;
  }
  return answer;
});

/** One `delegate_task` answer, read from its tool result. */
function startAnswerOf(result: McpSchema.CallToolResult): StartAnswer {
  const content = result.structuredContent as Record<string, unknown> | undefined;
  if (result.isError === true || content === undefined || typeof content.taskId !== "string") {
    const text = result.content
      .flatMap((part) => (part.type === "text" ? [part.text] : []))
      .join(" ");
    return refused(
      typeof content?.code === "string" ? content.code : "orchestration_error",
      typeof content?.message === "string" ? content.message : text || "delegate_task refused",
    );
  }
  return {
    type: "started",
    taskId: content.taskId,
    childThreadId: typeof content.childThreadId === "string" ? content.childThreadId : null,
  };
}

const SETTLED = new Set(["completed", "failed", "cancelled", "interrupted"]);

export interface FillerChild {
  readonly taskId: string;
  readonly title: string | null;
  readonly status: string;
  readonly live: boolean;
  /** The linked machine it runs on; null: this one. */
  readonly environmentId: string | null;
  readonly childThreadId: string | null;
  readonly providerInstanceId: string;
  readonly model: string | null;
}

/** The parent's own (app-owned) children, newest state as the store has it. */
export const childrenOf = (
  subagents: ReadonlyArray<{
    readonly id: string;
    readonly origin: string;
    readonly title: string | null;
    readonly status: string;
    readonly childThreadId: string | null;
    readonly providerInstanceId: string;
    readonly model: string | null;
    readonly remoteChild?: { readonly environmentId: string } | undefined;
  }>,
): ReadonlyArray<FillerChild> =>
  subagents
    .filter((task) => task.origin === "app_owned")
    .map((task) => ({
      taskId: task.id,
      title: task.title,
      status: task.status,
      live: !SETTLED.has(task.status),
      environmentId: task.remoteChild?.environmentId ?? null,
      childThreadId: task.childThreadId,
      providerInstanceId: task.providerInstanceId,
      model: task.model,
    }));

const json = (status: number, body: unknown) =>
  HttpServerResponse.jsonUnsafe(body, { status, headers: { "cache-control": "no-store" } });
const notFound = () => HttpServerResponse.text("Not Found", { status: 404 });

type Auth = Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateHttpRequest">;

/** The caller's session has `scope`: null; else the answer to give. */
const admitCaller = Effect.fn("FillerDelegate.admit")(function* (
  auth: Auth,
  request: HttpServerRequest.HttpServerRequest,
  scope: typeof AuthOrchestrationOperateScope | typeof AuthOrchestrationReadScope,
) {
  if (request.headers.origin !== undefined) return notFound();
  const session = yield* auth.authenticateHttpRequest(request).pipe(Effect.option);
  if (Option.isNone(session)) return HttpServerResponse.text("Unauthorized", { status: 401 });
  if (!session.value.scopes.includes(scope)) {
    return HttpServerResponse.text(`This session lacks ${scope}`, { status: 403 });
  }
  return null;
});

const handleStart = Effect.fn("FillerDelegate.handleStart")(function* (
  auth: Auth,
  world: FillerWorld,
) {
  const request = yield* HttpServerRequest.HttpServerRequest;
  const denied = yield* admitCaller(auth, request, AuthOrchestrationOperateScope);
  if (denied !== null) return denied;
  const text = yield* request.text.pipe(Effect.option);
  const input = Option.flatMap(text, decodeStart);
  if (Option.isNone(input)) {
    return json(400, {
      state: "refused",
      code: "bad_request",
      message:
        "body must be { parentThreadId, requestId, wake?, task } (task: delegate_task's input)",
    });
  }
  const answer = yield* runFillerStart(world, input.value).pipe(
    Effect.catchCause((cause) =>
      Effect.succeed(refused("orchestration_error", String(cause).slice(0, 300))),
    ),
  );
  if (answer.type === "started") {
    return json(200, {
      state: "started",
      taskId: answer.taskId,
      childThreadId: answer.childThreadId,
    });
  }
  return json(answer.code === "parent_missing" ? 404 : 409, {
    state: "refused",
    code: answer.code,
    message: answer.message,
  });
});

const decodeThreadId = Schema.decodeUnknownOption(ThreadId);

const handleList = Effect.fn("FillerDelegate.handleList")(function* (
  auth: Auth,
  threads: Pick<
    ThreadManagement.ThreadManagementService["Service"],
    "getThreadShell" | "getThreadRecords"
  >,
) {
  const request = yield* HttpServerRequest.HttpServerRequest;
  const denied = yield* admitCaller(auth, request, AuthOrchestrationReadScope);
  if (denied !== null) return denied;
  const threadId = decodeThreadId(
    new URL(request.url, "http://local.invalid").searchParams.get("parentThreadId"),
  );
  if (Option.isNone(threadId))
    return HttpServerResponse.text("parentThreadId is required", { status: 400 });
  const answer = yield* Effect.gen(function* () {
    const shell = yield* threads.getThreadShell(threadId.value);
    if (shell === null || shell.deletedAt !== null || shell.archivedAt !== null) return null;
    const records = yield* threads.getThreadRecords(threadId.value, ["subagents"]);
    return childrenOf(records.subagents);
  }).pipe(
    Effect.asSome,
    Effect.catchCause(() => Effect.succeedNone),
  );
  // A read that failed is no answer: the filler then starts nothing (it cannot count).
  if (Option.isNone(answer)) return HttpServerResponse.text("Unavailable", { status: 503 });
  if (answer.value === null) {
    return json(404, { state: "refused", code: "parent_missing", children: [] });
  }
  return json(200, { state: "ok", children: answer.value });
});

/** The route's world: this server's delegate_task, its thread store, the slot gate for the wake. */
const makeWorld = (
  threads: Pick<ThreadManagement.ThreadManagementService["Service"], "getThreadShell" | "dispatch">,
  receipts: Pick<CommandReceiptStore.CommandReceiptStoreV2["Service"], "getByCommandId">,
  gate: Option.Option<SlotGate.SlotGate["Service"]>,
): FillerWorld => ({
  parent: (threadId) =>
    threads.getThreadShell(threadId).pipe(
      Effect.map((shell) =>
        shell === null || shell.deletedAt !== null || shell.archivedAt !== null
          ? null
          : { threadId, providerInstanceId: ProviderInstanceId.make(shell.providerInstanceId) },
      ),
      Effect.orElseSucceed(() => null),
    ),
  delegate: Effect.fn("FillerDelegate.delegate")(function* (
    parent: FillerParent,
    task: OrchestratorMcpDelegateTaskInput,
  ) {
    const mcp = server;
    if (mcp === undefined) {
      return refused("orchestration_error", "this server's MCP tools are not ready yet");
    }
    const scope: McpInvocationContext.McpInvocationScope = {
      environmentId: (environmentId ?? "local") as EnvironmentId,
      requestNamespace: fillerNamespace(parent.threadId),
      thread: {
        threadId: parent.threadId,
        providerSessionId: fillerNamespace(parent.threadId),
        providerInstanceId: parent.providerInstanceId,
      },
      client: undefined,
      capabilities: new Set(["orchestration"]),
      issuedAt: yield* Clock.currentTimeMillis,
    };
    return yield* mcp
      .callTool({
        name: "delegate_task",
        arguments: encodeDelegate(task) as Record<string, unknown>,
      })
      .pipe(
        Effect.map(startAnswerOf),
        Effect.catch((error) =>
          Effect.succeed(
            refused(
              "orchestration_error",
              `delegate_task failed: ${String((error as { message?: unknown }).message ?? error)}`,
            ),
          ),
        ),
        Effect.provideService(McpInvocationContext.McpInvocationContext, scope),
        Effect.provideService(McpSchema.McpServerClient, serverClient),
      );
  }),
  wake: Effect.fn("FillerDelegate.wake")(
    function* (parent: FillerParent, requestId: string, text: string) {
      const commandId = CommandId.make(`slot-filler:wake:${requestId}`);
      const sent = yield* receipts.getByCommandId(commandId);
      if (Option.isSome(sent) && sent.value.status !== "rejected") return "sent" as const;
      const slot = yield* admitSend(gate, {
        callerThreadId: parent.threadId,
        threadId: parent.threadId,
        instanceId: parent.providerInstanceId,
        keepsHeld: true,
      });
      if (slot.type === "refuse") return "no_slot" as const;
      yield* threads.dispatch({
        type: "message.dispatch",
        commandId,
        threadId: parent.threadId,
        messageId: MessageId.make(`message:slot-filler:${requestId}`),
        text,
        attachments: [],
        dispatchMode: { type: "queue_after_active" },
        createdBy: "agent",
        creationSource: "server",
      });
      return "sent" as const;
    },
    Effect.orElseSucceed(() => "no_slot" as const),
  ),
  pause: Effect.sleep(Duration.seconds(2)),
});

/** The two routes, given the receipt store and the gate (tests give fakes). */
export const layerRoutes = Layer.unwrap(
  Effect.gen(function* () {
    const auth = yield* EnvironmentAuth.EnvironmentAuth;
    const threads = yield* ThreadManagement.ThreadManagementService;
    const receipts = yield* CommandReceiptStore.CommandReceiptStoreV2;
    const gate = yield* Effect.serviceOption(SlotGate.SlotGate);
    const world = makeWorld(threads, receipts, gate);
    return Layer.mergeAll(
      HttpRouter.add("POST", FILLER_DELEGATE_PATH, handleStart(auth, world)),
      HttpRouter.add("GET", FILLER_DELEGATE_PATH, handleList(auth, threads)),
    );
  }),
);

/** The two routes in the server's routes (fork/forkLayers.ts). */
export const layer = layerRoutes.pipe(
  Layer.provide(
    // The server's one gate and receipt store: layers memoize by reference.
    Layer.mergeAll(SlotGate.layer, CommandReceiptStore.layerFromApplicationReceipts),
  ),
);
