/**
 * Fork: `t3_thread_send` and `t3_thread_send_attachments` through the agent slot gate, on
 * upstream's real handlers and access rules and the server's real gate (`slotWorld.testkit.ts`).
 * An agent's message into a thread that shows no run starts one, so it takes a slot like
 * `delegate_task`; into a thread that shows a run it only queues. Refused, nothing is sent.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { describe, expect, it } from "@effect/vitest";
import {
  ChatImageAttachment,
  EnvironmentId,
  ProviderInstanceId,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Stream from "effect/Stream";
import { TestClock } from "effect/testing";

import * as AssetSigner from "../assets/AssetSigner.ts";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import { idleThreadProjection, liveThreadShell } from "../mcp/McpToolAccess.testkit.ts";
import * as OrchestratorMcpService from "../mcp/OrchestratorMcpService.ts";
import * as ThreadMetadataMcpService from "../mcp/ThreadMetadataMcpService.ts";
import * as AttachmentHandlers from "../mcp/toolkits/attachment/handlers.ts";
import { AttachmentToolkit } from "../mcp/toolkits/attachment/tools.ts";
import * as OrchestratorHandlers from "../mcp/toolkits/orchestrator/handlers.ts";
import { OrchestratorToolkit } from "../mcp/toolkits/orchestrator/tools.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinkRequests from "../peer/PeerLinkRequests.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";
import * as DocumentText from "../provider/DocumentText.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as AgentSlotRoutes from "./agentSlotRoutes.ts";
import { type SlotWorld, slotGateOver, slotWorld } from "./slotWorld.testkit.ts";

/** `liveThreadShell`'s provider instance. */
const INSTANCE = ProviderInstanceId.make("codex");
const CALLER = ThreadId.make("thread:sender");
const TARGET = ThreadId.make("thread:receiver");
const OTHER = ThreadId.make("thread:receiver-two");
const BUSY = ThreadId.make("thread:busy");

type Settings = Parameters<typeof ServerSettings.layerTest>[0];

const slots = (agentSlots: object, more: object = {}) =>
  ({ agentSlots: { enabled: true, startGraceSeconds: 30, ...agentSlots }, ...more }) as Settings;
const ONE_SLOT = slots({ maxPerMachine: 1 });
const TWO_SLOTS = slots({ maxPerMachine: 2 });
const WAITS_ON = slots({ maxPerMachine: 1 }, { waitFor: { enabled: true } });

/** An attachment the receiver already has, so `t3_thread_send_attachments` may send it again. */
const STORED = ChatImageAttachment.make({
  type: "image",
  id: "owned-image",
  name: "original.png",
  mimeType: "image/png",
  sizeBytes: 12,
});

interface Options {
  readonly settings?: Settings;
  /** Without: the host runs no gate. */
  readonly slots?: SlotWorld;
  /** `false`: the owner's client (or a linked machine) sends, not a thread here. */
  readonly caller?: boolean;
}

const TOOLS = ["t3_thread_send", "t3_thread_send_attachments"] as const;
type Tool = (typeof TOOLS)[number];

/**
 * Upstream's handlers of both send tools, every message they send recorded. The thread tools
 * read every receiver as idle; whether it shows a run is the slot world's to say.
 */
const harness = (options: Options = {}) => {
  const sent: Array<ThreadId> = [];
  const threadCaller = options.caller !== false;
  const shellOf = (id: ThreadId) =>
    id === CALLER ? liveThreadShell(id) : liveThreadShell(id, { activeRunId: null });
  const base = Layer.mergeAll(
    ServerSettings.layerTest(options.settings ?? ONE_SLOT),
    Layer.succeed(McpInvocationContext.McpInvocationContext, {
      environmentId: EnvironmentId.make("environment-here"),
      requestNamespace: "thread:sender:session",
      thread: threadCaller
        ? { threadId: CALLER, providerSessionId: "session", providerInstanceId: INSTANCE }
        : undefined,
      client: threadCaller
        ? undefined
        : { sessionId: "session-1", label: "Owner", access: "full-access" },
      issuedAt: 0,
      capabilities: new Set(["orchestration" as const]),
    }),
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: (id) => Effect.succeed(shellOf(id)),
      getProjectThreadRecords: ({ threadId }) =>
        Effect.succeed({
          ...idleThreadProjection(shellOf(threadId)),
          messages: [{ attachments: [STORED] }],
        } as never),
      sendToThread: (input) =>
        Effect.sync(() => {
          sent.push(input.threadId);
          return {
            run: { id: RunId.make(`run:${input.threadId}`), status: "running" },
            message: { attachments: input.attachments },
          } as never;
        }),
    }),
    Layer.mock(OrchestratorMcpService.OrchestratorMcpService)({
      sendToThread: (_scope, input) =>
        Effect.sync(() => {
          sent.push(input.threadId as ThreadId);
          return {
            threadId: input.threadId,
            messageId: `message:${input.threadId}`,
            runId: `run:${input.threadId}`,
            status: "running",
            delivery: "started",
          } as never;
        }),
    }),
    Layer.mock(ThreadMetadataMcpService.ThreadMetadataMcpService)({}),
    Layer.mock(PeerForwarding.PeerForwarding)({}),
    Layer.mock(PeerLinks.PeerLinks)({}),
    Layer.mock(PeerLinkRequests.PeerLinkRequests)({}),
    Layer.mock(RemoteDelegation.RemoteDelegation)({}),
    AssetSigner.layer.pipe(Layer.provideMerge(ServerSecretStore.layer)),
    DocumentText.layer,
  ).pipe(
    Layer.provideMerge(
      ServerConfig.layerTest(process.cwd(), { prefix: "t3-send-slot-" }).pipe(
        Layer.provide(NodeServices.layer),
      ),
    ),
    Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
  );
  // One gate for the whole test, so it keeps its memory from one send to the next.
  let gate: SlotGate.SlotGate["Service"] | undefined;
  const dependenciesOf = Effect.gen(function* () {
    if (options.slots === undefined) return base;
    gate ??= yield* SlotGate.SlotGate.pipe(
      Effect.provide(slotGateOver(options.slots, INSTANCE).pipe(Layer.provide(base))),
    );
    return Layer.merge(base, Layer.succeed(SlotGate.SlotGate, gate));
  });
  const send = (tool: Tool, threadId: ThreadId = TARGET) =>
    Effect.gen(function* () {
      const dependencies = yield* dependenciesOf;
      if (tool === "t3_thread_send") {
        const toolkit = yield* OrchestratorToolkit.pipe(
          Effect.provide(
            McpToolAccess.HandlersLayer.layer(OrchestratorHandlers.layer).pipe(
              Layer.provide(dependencies),
            ),
          ),
        );
        return yield* toolkit
          .handle("t3_thread_send", { threadId, message: "Next step." } as never)
          .pipe(Stream.unwrap, Stream.runCollect, Effect.provide(dependencies));
      }
      const toolkit = yield* AttachmentToolkit.pipe(
        Effect.provide(
          McpToolAccess.HandlersLayer.layer(AttachmentHandlers.layer).pipe(
            Layer.provide(dependencies),
          ),
        ),
      );
      return yield* toolkit
        .handle("t3_thread_send_attachments", {
          threadId,
          message: "See the file.",
          attachments: [STORED],
        } as never)
        .pipe(Stream.unwrap, Stream.runCollect, Effect.provide(dependencies));
    });
  return { sent, send };
};

/** `code: message` of a refused call, or `undefined` for one that went through. */
const refusal = (chunks: Iterable<unknown>): string | undefined => {
  const last = [...chunks].at(-1) as
    | { readonly isFailure: boolean; readonly result: { code?: string; message?: string } }
    | undefined;
  return last?.isFailure === true ? `${last.result.code}: ${last.result.message}` : undefined;
};

describe.each(TOOLS)("%s through the agent slot gate (#711)", (tool) => {
  it.effect("full: refused slots_full with delegate_task's wording; nothing is sent", () =>
    Effect.gen(function* () {
      const { sent, send } = harness({ slots: slotWorld([BUSY]) });
      const message = refusal(yield* send(tool));
      expect(message).toBe(
        "slots_full: This call sent nothing to thread thread:receiver. No free agent slot: 1 agent is " +
          "running on this machine, and the limit is 1. Try again later. Or do the work yourself.",
      );
      expect(sent).toEqual([]);
      // Waits on: told to wait for a slot and end its turn, as delegate_task says.
      const waits = harness({ settings: WAITS_ON, slots: slotWorld([BUSY]) });
      expect(refusal(yield* waits.send(tool))).toContain(
        'Call wait_for {"kind":"slot","slot":{}} and end your turn',
      );
      expect(waits.sent).toEqual([]);
    }),
  );

  it.effect("free: sent, and the run it starts fills the slot", () =>
    Effect.gen(function* () {
      const world = slotWorld();
      const { sent, send } = harness({ slots: world });
      expect(refusal(yield* send(tool))).toBeUndefined();
      expect(sent).toEqual([TARGET]);
      // Its run shows no run yet: a start into another idle thread finds no slot.
      expect(refusal(yield* send(tool, OTHER))).toMatch(/^slots_full: This call sent nothing/);
      // It shows now: counted once, as running.
      world.running = [TARGET];
      expect(refusal(yield* send(tool, OTHER))).toMatch(/^slots_full: This call sent nothing/);
      expect(sent).toEqual([TARGET]);
    }),
  );

  it.effect("into a thread that shows a run it only queues: sent when full, takes no slot", () =>
    Effect.gen(function* () {
      const { sent, send } = harness({ slots: slotWorld([TARGET]) });
      expect(refusal(yield* send(tool))).toBeUndefined();
      // Runs again (an agent's retry, or its next message): queued each time.
      expect(refusal(yield* send(tool))).toBeUndefined();
      expect(sent).toEqual([TARGET, TARGET]);
    }),
  );

  it.effect("the race: a run that ends before the message lands leaves it holding the slot", () =>
    Effect.gen(function* () {
      const world = slotWorld([TARGET, BUSY]);
      const { sent, send } = harness({ settings: TWO_SLOTS, slots: world });
      expect(refusal(yield* send(tool))).toBeUndefined();
      // The run ended just after the check: the queued message starts the next one, in the slot.
      world.running = [BUSY];
      expect(refusal(yield* send(tool, OTHER))).toMatch(/^slots_full: /);
      // Never shown within the start grace: the slot frees.
      yield* TestClock.adjust("30 seconds");
      expect(refusal(yield* send(tool, OTHER))).toBeUndefined();
      expect(sent).toEqual([TARGET, OTHER]);
    }),
  );

  it.effect("off, or a host that runs no gate: sent as upstream, nothing read", () =>
    Effect.gen(function* () {
      const world = slotWorld([BUSY]);
      const off = harness({
        settings: { agentSlots: { mode: "fixed" } } as Settings,
        slots: world,
      });
      expect(refusal(yield* off.send(tool))).toBeUndefined();
      expect(off.sent).toEqual([TARGET]);
      expect(world.reads).toBe(0);
      const none = harness();
      expect(refusal(yield* none.send(tool))).toBeUndefined();
      expect(none.sent).toEqual([TARGET]);
    }),
  );

  it.effect("a count that cannot be read refuses slots_full; nothing is sent", () =>
    Effect.gen(function* () {
      const world = slotWorld([TARGET]);
      world.fails = true;
      const { sent, send } = harness({ slots: world });
      // Fails closed even for a thread that runs: running or not cannot be told.
      expect(refusal(yield* send(tool))).toMatch(
        /^slots_full: This call sent nothing to thread thread:receiver\. No agent was started: the slot gate could not read the thread list/,
      );
      expect(sent).toEqual([]);
      // Readable again: the retry goes through, once.
      world.fails = false;
      expect(refusal(yield* send(tool))).toBeUndefined();
      expect(sent).toEqual([TARGET]);
    }),
  );

  it.effect("no thread here asks (the owner's client, a linked machine): told what happened", () =>
    Effect.gen(function* () {
      const { sent, send } = harness({ caller: false, slots: slotWorld([BUSY]) });
      expect(refusal(yield* send(tool))).toBe(
        "slots_full: This call sent nothing to thread thread:receiver. No agent was started: the " +
          "machine asked to start it has no free agent slot (1 agent is running there, and the limit is 1).",
      );
      expect(sent).toEqual([]);
    }),
  );
});

/** {@link AgentSlotRoutes.checkSendSlot} alone, over a receiver the thread tools read as given. */
const check = (
  world: SlotWorld,
  shell: ReturnType<ThreadManagement.ThreadManagementService["Service"]["getThreadShell"]>,
) =>
  AgentSlotRoutes.checkSendSlot(CALLER, TARGET).pipe(
    Effect.flip,
    Effect.map((failure) => `${failure.code}: ${failure.message}`),
    Effect.orElseSucceed(() => undefined),
    Effect.provide(
      slotGateOver(world, INSTANCE).pipe(
        Layer.provideMerge(
          Layer.mergeAll(
            ServerSettings.layerTest(ONE_SLOT),
            Layer.mock(ThreadManagement.ThreadManagementService)({ getThreadShell: () => shell }),
            NodeServices.layer,
          ),
        ),
      ),
    ),
  );

describe("checkSendSlot: a receiver the gate cannot judge (#711)", () => {
  it.effect(
    "gone, deleted or archived: left to the send, which refuses it with its own error",
    () =>
      Effect.gen(function* () {
        const world = slotWorld([BUSY]);
        const idle = liveThreadShell(TARGET, { activeRunId: null });
        const at = DateTime.makeUnsafe("2026-01-02T00:00:00.000Z");
        expect(yield* check(world, Effect.succeed(null))).toBeUndefined();
        expect(yield* check(world, Effect.succeed({ ...idle, deletedAt: at }))).toBeUndefined();
        expect(yield* check(world, Effect.succeed({ ...idle, archivedAt: at }))).toBeUndefined();
        expect(world.reads).toBe(0);
        // A live one is judged: full refuses.
        expect(yield* check(world, Effect.succeed(idle))).toMatch(/^slots_full: /);
      }),
  );

  it.effect("unreadable: refused, nothing sent, the gate not asked", () =>
    Effect.gen(function* () {
      const world = slotWorld();
      const message = yield* check(
        world,
        Effect.fail(new Orchestrator.OrchestratorProjectionError({ threadId: TARGET })),
      );
      expect(message).toMatch(
        /^orchestration_error: This call sent nothing to thread thread:receiver: it could not be read to check for a free agent slot \(Failed to load orchestration projection for thread thread:receiver\.\)\.$/,
      );
      expect(world.reads).toBe(0);
    }),
  );
});
