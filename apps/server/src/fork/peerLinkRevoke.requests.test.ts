/**
 * Fork: a link the box revoked goes through the link card again. The laptop is its real
 * orchestrator and toolkit, the box its real descriptor and MCP OAuth on a socket, as in
 * PeerLinkRequests.test.ts, whose harness this copies (upstream's test file stays as it is).
 */
import { NodeHttpServer } from "@effect/platform-node";
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  CommandId,
  EventId,
  MessageId,
  NodeId,
  OrchestratorMcpFailure,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ProviderThreadId,
  type RuntimeMode,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import { expect, it } from "@effect/vitest";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";
import { McpSchema, McpServer, Tool, Toolkit } from "effect/ai";

import * as ServerConfig from "../config.ts";
import * as McpHttpServer from "../mcp/McpHttpServer.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import { CodexProviderCapabilitiesV2 } from "../orchestration-v2/Adapters/CodexAdapterV2.ts";
import * as EventSink from "../orchestration-v2/EventSink.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import type * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as ProviderAdapterRegistry from "../orchestration-v2/ProviderAdapterRegistry.ts";
import * as ProviderContinuationRequests from "@t3tools/provider-core/server/ProviderContinuationRequests";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ProviderReplayHarness from "../orchestration-v2/testkit/ProviderReplayHarness.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as ProviderRegistry from "../provider/ProviderRegistry.ts";
import * as ScheduledTaskService from "../scheduledTasks/ScheduledTaskService.ts";
import * as SecretRequests from "../secrets/SecretRequests.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadLaunch from "../orchestration-v2/ThreadLaunchService.ts";
import * as GitVcsDriver from "../vcs/GitVcsDriver.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinkRequests from "../peer/PeerLinkRequests.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import * as PeerMcpClient from "../peer/PeerMcpClient.ts";
import {
  descriptorOf,
  layerLinkingEnvironment,
  linkTo,
  servePeer,
  type ServedPeer,
} from "../peer/PeerLinks.testkit.ts";
import * as RemoteDelegation from "../peer/RemoteDelegation.ts";

const laptop = descriptorOf("environment-laptop", "Laptop");
const box = descriptorOf("environment-box", "Box");
const instanceId = ProviderInstanceId.make("codex");
const driver = ProviderDriverKind.make("codex");
const modelSelection = { instanceId, model: "gpt-5.4" };
const threadId = ThreadId.make("thread:laptop");
const runId = RunId.make("run:laptop");
const rootNode = NodeId.make("node:laptop:root");

/** The box needs no tools of its own: linking only signs in to its /mcp. */
const NoTools = Toolkit.make(
  Tool.make("noop", { success: Schema.Struct({}), failure: OrchestratorMcpFailure }),
);
const serveBox = servePeer(
  box,
  McpHttpServer.toolkitRegistration(
    NoTools,
    McpToolAccess.toLayer(NoTools, { noop: McpToolAccess.reads(() => Effect.succeed({})) }),
  ),
);

const adapter = {
  instanceId,
  driver,
  getCapabilities: () => Effect.succeed(CodexProviderCapabilitiesV2),
  planSelectionTransition: () => Effect.succeed({ type: "apply_on_next_turn" as const }),
  openSession: () => Effect.die("No provider process needed"),
} as ProviderAdapter.ProviderAdapterV2["Service"];

const mcpClient = McpSchema.McpServerClient.of({
  clientId: 1,
  protocolVersion: "2025-06-18",
  clientCapabilities: {},
  clientInfo: { name: "peer-link-requests", version: "1" },
  initializePayload: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "peer-link-requests", version: "1" },
  },
  getClient: Effect.die("unused"),
});

const scope: McpInvocationContext.McpInvocationScope = {
  environmentId: laptop.environmentId,
  requestNamespace: "provider-session:laptop",
  thread: {
    threadId,
    providerSessionId: "provider-session:laptop",
    providerInstanceId: instanceId,
  },
  client: undefined,
  capabilities: new Set(["orchestration"]),
  issuedAt: 1,
};

/** A real orchestrator and links; the wrapper can hold a link in progress. */
const makeLaptopWith = (
  wrapLinks?: (links: PeerLinks.PeerLinks["Service"]) => PeerLinks.PeerLinks["Service"],
) =>
  Effect.gen(function* () {
    const linking = yield* layerLinkingEnvironment(laptop).pipe(Layer.build);
    const answering =
      wrapLinks === undefined
        ? linking
        : Context.add(
            linking,
            PeerLinks.PeerLinks,
            wrapLinks(Context.get(linking, PeerLinks.PeerLinks)),
          );
    // The orchestrator's own database, apart from the one links are kept in.
    const database = yield* SqlitePersistence.layerMemory.pipe(Layer.build);
    const layerDatabase = Layer.succeedContext(database);
    const layerOrchestrator = Layer.mergeAll(
      layerDatabase,
      ProjectionStore.layer.pipe(Layer.provide(layerDatabase)),
      ProviderReplayHarness.layerWithRegistry(
        { name: "peer-link-requests" },
        ProviderAdapterRegistry.layerFromAdapters([adapter]),
        { databaseLayer: layerDatabase, runEffectWorker: false },
      ),
    ).pipe(
      Layer.provide(
        Layer.succeed(ProviderContinuationRequests.ProviderContinuationRequests, {
          offer: () => Effect.void,
          take: Effect.never,
        }),
      ),
    );
    const layerThreads = ThreadManagement.layer.pipe(
      Layer.provide(CommandReceiptStore.layer.pipe(Layer.provide(layerDatabase))),
      Layer.provideMerge(Layer.merge(layerOrchestrator, ServerSettings.layerTest())),
    );
    const layerHere = McpHttpServer.layerOrchestratorToolkit.pipe(
      Layer.provideMerge(McpHttpServer.layerInProcessMcpServer),
      Layer.provideMerge(PeerLinkRequests.layer),
      Layer.provide(Layer.mock(PeerForwarding.PeerForwarding)({})),
      Layer.provide(Layer.mock(RemoteDelegation.RemoteDelegation)({})),
      Layer.provide(Layer.succeedContext(answering)),
      Layer.provide(NodeCrypto.layer),
      Layer.provideMerge(layerThreads),
      Layer.provide(Layer.mock(ProviderRegistry.ProviderRegistry)({})),
      Layer.provide(Layer.mock(ProviderAdapterRegistry.ProviderAdapterRegistryV2)({})),
      Layer.provide(Layer.mock(ScheduledTaskService.ScheduledTaskService)({})),
      Layer.provide(Layer.mock(ProjectService.ProjectService)({})),
      Layer.provide(Layer.mock(SecretRequests.SecretRequests)({})),
      Layer.provide(Layer.mock(ThreadLaunch.ThreadLaunchService)({})),
      Layer.provide(Layer.mock(GitVcsDriver.GitVcsDriver)({})),
      Layer.provide(
        ServerConfig.layerTest(process.cwd(), { prefix: "t3-peer-link-requests-" }).pipe(
          Layer.provide(NodeServices.layer),
        ),
      ),
      Layer.provide(NodeServices.layer),
      Layer.fresh,
    );
    const here = yield* Layer.build(layerHere);
    const server = Context.get(here, McpServer.McpServer);
    return {
      links: Context.get(linking, PeerLinks.PeerLinks),
      linksSql: Context.get(linking, SqlClient.SqlClient),
      peers: Context.get(linking, PeerMcpClient.PeerMcpClient),
      requests: Context.get(here, PeerLinkRequests.PeerLinkRequests),
      orchestrator: Context.get(here, Orchestrator.OrchestratorV2),
      sink: Context.get(here, EventSink.EventSinkV2),
      projections: Context.get(here, ProjectionStore.ProjectionStoreV2),
      sql: Context.get(database, SqlClient.SqlClient),
      call: (args: Record<string, unknown>, callScope = scope) =>
        server
          .callTool({ name: "t3_environment_link", arguments: args })
          .pipe(
            Effect.provideService(McpInvocationContext.McpInvocationContext, callScope),
            Effect.provideService(McpSchema.McpServerClient, mcpClient),
          ),
    };
  });
const makeLaptop = makeLaptopWith();
type Laptop = Effect.Success<typeof makeLaptop>;

/** The laptop's thread, mid-turn when its agent asks, in `runtimeMode`. */
const seedThread = (
  a: Laptop,
  runtimeMode: RuntimeMode = "full-access",
  linkOrigin?: { readonly sessionId: string; readonly label: string },
) =>
  Effect.gen(function* () {
    const now = yield* DateTime.now;
    const providerThreadId = ProviderThreadId.make("provider-thread:laptop");
    yield* a.orchestrator.dispatch({
      type: "thread.create",
      createdBy: "user",
      creationSource: "web",
      commandId: CommandId.make("command:create:laptop"),
      threadId,
      projectId: ProjectId.make("project:laptop"),
      title: "Laptop",
      modelSelection,
      runtimeMode,
      interactionMode: "default",
      branch: null,
      worktreePath: null,
      ...(linkOrigin === undefined ? {} : { linkOrigin }),
    });
    yield* a.sink.write({
      commandId: CommandId.make("command:seed:laptop"),
      events: [
        {
          id: EventId.make("event:seed-provider-thread"),
          type: "provider-thread.updated",
          threadId,
          driver,
          providerInstanceId: instanceId,
          occurredAt: now,
          payload: {
            id: providerThreadId,
            driver,
            providerInstanceId: instanceId,
            providerSessionId: null,
            appThreadId: threadId,
            ownerNodeId: rootNode,
            nativeThreadRef: { driver, nativeId: "native:laptop", strength: "strong" },
            nativeConversationHeadRef: null,
            status: "active",
            firstRunOrdinal: 1,
            lastRunOrdinal: 1,
            handoffIds: [],
            forkedFrom: null,
            createdAt: now,
            updatedAt: now,
          },
        },
        {
          id: EventId.make("event:seed-node"),
          type: "node.updated",
          threadId,
          runId,
          nodeId: rootNode,
          driver,
          providerInstanceId: instanceId,
          occurredAt: now,
          payload: {
            id: rootNode,
            threadId,
            runId,
            parentNodeId: null,
            rootNodeId: rootNode,
            kind: "root_turn",
            status: "running",
            countsForRun: true,
            providerThreadId,
            providerTurnId: null,
            nativeItemRef: null,
            runtimeRequestId: null,
            checkpointScopeId: null,
            startedAt: now,
            completedAt: null,
          },
        },
        {
          id: EventId.make("event:seed-run"),
          type: "run.updated",
          threadId,
          runId,
          nodeId: rootNode,
          providerInstanceId: instanceId,
          occurredAt: now,
          payload: {
            id: runId,
            threadId,
            ordinal: 1,
            providerInstanceId: instanceId,
            modelSelection,
            providerThreadId,
            userMessageId: MessageId.make("message:seed-user"),
            rootNodeId: rootNode,
            activeAttemptId: null,
            status: "running",
            requestedAt: now,
            startedAt: now,
            completedAt: null,
            checkpointId: null,
            contextHandoffId: null,
          },
        },
      ],
    });
  });

const cardOf = (a: Laptop) =>
  a.orchestrator
    .getThreadProjection(threadId)
    .pipe(
      Effect.map((projection) => projection.turnItems.find((item) => item.type === "link_request")),
    );

/** The open card the agent's call left, as the user's client sees it. */
const pendingCard = (a: Laptop) =>
  Effect.gen(function* () {
    const card = yield* cardOf(a);
    expect(card).toMatchObject({ status: "waiting", linkStatus: "pending" });
    return card as Extract<NonNullable<typeof card>, { readonly type: "link_request" }>;
  });

const withBoth = <A, E, R>(body: (a: Laptop, b: ServedPeer) => Effect.Effect<A, E, R>) =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBox;
      const a = yield* makeLaptop;
      return yield* body(a, b);
    }),
  ).pipe(Effect.provide(NodeHttpServer.layerTest));

const linkCards = (a: Laptop) =>
  a.orchestrator
    .getThreadProjection(threadId)
    .pipe(
      Effect.map((projection) =>
        projection.turnItems.flatMap((item) => (item.type === "link_request" ? [item] : [])),
      ),
    );

const noop = (a: Laptop) =>
  a.peers.call({
    environmentId: box.environmentId,
    tool: "noop",
    arguments: {},
    limits: { runtimeMode: "full-access", interactionMode: "default" },
    success: Schema.Struct({}),
  });

/** The box revokes the laptop's link there; the laptop's next call finds out. */
const revokeThere = (a: Laptop, b: ServedPeer) =>
  Effect.gen(function* () {
    yield* noop(a);
    const [session] = yield* b.linkedSessions;
    yield* b.auth.revokeSession(session!.sessionId);
    expect((yield* noop(a).pipe(Effect.flip)).code).toBe("capability_denied");
    // The box turned the link away, so it is over here too: no list calls it reachable.
    expect((yield* a.links.list).map((link) => link.status)).toEqual(["expired"]);
  });

it.live("a revoked link's card, opened by its environmentId, links again with a new code", () =>
  withBoth((a, b) =>
    Effect.gen(function* () {
      yield* seedThread(a);
      yield* linkTo(a.links, b);
      yield* revokeThere(a, b);
      const before = yield* a.links.get(box.environmentId);

      // Named by its environmentId, the ended link is not "linked": the call opens a card.
      const named = yield* a.call({ environmentId: box.environmentId });
      expect(named.structuredContent).toMatchObject({ status: "pending" });
      const card = yield* pendingCard(a);
      const pairing = yield* b.auth.issuePairingCredential();
      yield* a.requests.answer({
        threadId,
        turnItemId: card.id,
        answer: { type: "link", urls: [b.url], access: "auto", pairingCode: pairing.credential },
      });
      expect(yield* cardOf(a)).toMatchObject({ linkStatus: "linked" });
      // The new code replaced the stored credential: calls work and the link is live again.
      expect(yield* noop(a)).toEqual({});
      expect((yield* a.links.list).map((link) => link.status)).toEqual(["reachable"]);
      const after = yield* a.links.get(box.environmentId);
      expect(after._tag === "Some" && before._tag === "Some").toBe(true);
      if (after._tag === "Some" && before._tag === "Some") {
        expect(DateTime.formatIso(after.value.linkedAt)).not.toBe(
          DateTime.formatIso(before.value.linkedAt),
        );
      }
    }),
  ),
);

it.live("a revoked link's card refuses use-existing; a hint-only card links again", () =>
  withBoth((a, b) =>
    Effect.gen(function* () {
      yield* seedThread(a);
      yield* linkTo(a.links, b);
      yield* revokeThere(a, b);

      const named = yield* a.call({ environmentId: box.environmentId });
      expect(named.structuredContent).toMatchObject({ status: "pending" });
      const first = yield* pendingCard(a);
      // Keeping the revoked link is no answer.
      yield* a.requests.answer({
        threadId,
        turnItemId: first.id,
        answer: { type: "use-existing", environmentId: box.environmentId },
      });
      expect(yield* cardOf(a)).toMatchObject({ linkStatus: "failed" });

      yield* a.call({ hint: "box" });
      const second = (yield* linkCards(a)).find((card) => card.linkStatus === "pending")!;
      const pairing = yield* b.auth.issuePairingCredential();
      yield* a.requests.answer({
        threadId,
        turnItemId: second.id,
        answer: { type: "link", urls: [b.url], access: "auto", pairingCode: pairing.credential },
      });
      expect((yield* linkCards(a)).map((card) => card.linkStatus)).toEqual(["failed", "linked"]);
      expect(yield* noop(a)).toEqual({});
      expect((yield* a.links.list).map((link) => link.status)).toEqual(["reachable"]);
    }),
  ),
);
