/**
 * Fork (placement row P3): `t3_environment_read` naming a linked machine returns that machine's
 * facts over a real link; one on an older T3 leaves them out (no facts, never a failed read).
 */
import { NodeHttpServer } from "@effect/platform-node";
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { expect, it } from "@effect/vitest";
import { ProviderInstanceId, ServerSettings, ThreadId } from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";
import { McpSchema, McpServer } from "effect/ai";

import * as McpHttpServer from "../mcp/McpHttpServer.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import { liveThreadShell } from "../mcp/McpToolAccess.testkit.ts";
import * as EnvironmentHandlers from "../mcp/toolkits/environment/handlers.ts";
import { EnvironmentToolkit } from "../mcp/toolkits/environment/tools.ts";
import * as ThreadCommandExecutor from "../orchestration-v2/ThreadCommandExecutor.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../peer/PeerForwarding.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import {
  descriptorOf,
  layerLinkingEnvironment,
  linkTo,
  servePeer,
} from "../peer/PeerLinks.testkit.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as Settings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";

const laptop = descriptorOf("environment-laptop", "Laptop");
const box = descriptorOf("environment-box", "Box");
const fromFile = Schema.decodeUnknownSync(ServerSettings);

const hostResourcesOf = (cpuCount: number) => ({
  sampledAt: 1_700_000_000_000,
  cpuUtilization: 0.25,
  cpuCount,
  availableMemoryBytes: 8 * 1024 ** 3,
  totalMemoryBytes: 16 * 1024 ** 3,
});

/**
 * One machine's sampler, slot gate and settings: `maxPerMachine` slots, `used` of them in use. A
 * served peer's requests carry its own settings service, which wins over this one there.
 */
const layerMachine = (cpuCount: number, maxPerMachine: number, used: number) =>
  Layer.mergeAll(
    Layer.mock(HostResources.HostResources)({ read: Effect.succeed(hostResourcesOf(cpuCount)) }),
    Layer.mock(SlotGate.SlotGate)({
      usage: Effect.succeed({
        machine: { used, max: maxPerMachine },
        accounts: [],
        freeMemoryMb: 8192,
      }),
    }),
    Layer.mock(Settings.ServerSettingsService)({
      getSettings: Effect.succeed(
        fromFile({ linkedEnvironmentsEnabled: true, agentSlots: { enabled: true, maxPerMachine } }),
      ),
    }),
  );

const layerCallerThreads = Layer.mock(ThreadManagement.ThreadManagementService)({
  getThreadShell: (threadId) => Effect.succeed(liveThreadShell(threadId)),
});

/** The environment toolkit as a T3 from before P3 serves it: identity and preferences only. */
const layerOlderEnvironmentHandlers = McpToolAccess.toLayer(EnvironmentToolkit, {
  t3_environment_read: McpToolAccess.reads(() =>
    Effect.gen(function* () {
      const settings = yield* Settings.ServerSettingsService;
      return {
        environmentId: box.environmentId,
        label: box.label,
        serverVersion: box.serverVersion,
        platform: box.platform,
        preferences: EnvironmentHandlers.preferences(
          yield* settings.getSettings.pipe(Effect.orDie),
        ),
      };
    }),
  ),
  t3_environment_preferences_update: McpToolAccess.writesEnvironment(() =>
    Effect.die("not called"),
  ),
});

/** The box's `/mcp` with the environment toolkit, behind its real OAuth. */
const serveBox = (older: boolean) =>
  servePeer(
    box,
    McpHttpServer.toolkitRegistration(
      EnvironmentToolkit,
      older ? layerOlderEnvironmentHandlers : EnvironmentHandlers.layer,
    ).pipe(
      Layer.provide(ThreadCommandExecutor.layer),
      Layer.provide(layerCallerThreads),
      Layer.provide(layerMachine(8, 4, 3)),
    ),
  );

const mcpClient = McpSchema.McpServerClient.of({
  clientId: 1,
  protocolVersion: "2025-06-18",
  clientCapabilities: {},
  clientInfo: { name: "environment-facts-test", version: "1" },
  initializePayload: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "environment-facts-test", version: "1" },
  },
  getClient: Effect.die("unused"),
});

/** The laptop: the environment toolkit with forwarding, called from one of its threads. */
const makeLaptop = Effect.gen(function* () {
  const linking = yield* layerLinkingEnvironment(laptop).pipe(Layer.build);
  const here = yield* McpHttpServer.toolkitRegistration(
    EnvironmentToolkit,
    EnvironmentHandlers.layer,
  ).pipe(
    Layer.provideMerge(McpHttpServer.layerInProcessMcpServer),
    Layer.provide(ThreadCommandExecutor.layer),
    Layer.provide(PeerForwarding.layer),
    Layer.provide(layerMachine(4, 2, 0)),
    Layer.provide(Layer.succeedContext(linking)),
    Layer.provide(layerCallerThreads),
    Layer.provide(NodeCrypto.layer),
    Layer.provide(NodeServices.layer),
    Layer.fresh,
    Layer.build,
  );
  const server = Context.get(here, McpServer.McpServer);
  const read = (environmentId: string) =>
    server.callTool({ name: "t3_environment_read", arguments: { environmentId } }).pipe(
      Effect.provideService(McpInvocationContext.McpInvocationContext, {
        environmentId: laptop.environmentId,
        requestNamespace: "provider-session:thread:laptop",
        thread: {
          threadId: ThreadId.make("thread:laptop"),
          providerSessionId: "provider-session:thread:laptop",
          providerInstanceId: ProviderInstanceId.make("codex"),
        },
        client: undefined,
        capabilities: new Set(["orchestration" as const]),
        issuedAt: 1,
      }),
      Effect.provideService(McpSchema.McpServerClient, mcpClient),
    );
  return { links: Context.get(linking, PeerLinks.PeerLinks), read };
});

it.effect("a linked machine's read carries that machine's hostResources and placement", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBox(false);
      // The box's own settings, as its server reads them for each request.
      yield* b.settings.updateSettings({ agentSlots: { enabled: true, maxPerMachine: 4 } });
      const a = yield* makeLaptop;
      yield* linkTo(a.links, b, "auto");

      const there = yield* a.read(box.environmentId);
      expect(there.isError).toBe(false);
      expect(there.structuredContent).toMatchObject({
        environmentId: box.environmentId,
        hostResources: hostResourcesOf(8),
        placement: { labels: [], available: true, runningAgents: 3, slotsFree: 1 },
      });
      expect(
        (there.structuredContent as { placement: { factsAt: unknown } }).placement.factsAt,
      ).toBeTypeOf("number");

      const here = yield* a.read(laptop.environmentId);
      expect(here.structuredContent).toMatchObject({
        environmentId: laptop.environmentId,
        hostResources: hostResourcesOf(4),
        placement: { labels: [], available: true, runningAgents: 0, slotsFree: 2 },
      });
    }),
  ).pipe(Effect.provide(NodeHttpServer.layerTest)),
);

it.effect("a linked machine on an older T3 leaves the facts out: no facts, not a failure", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBox(true);
      const a = yield* makeLaptop;
      yield* linkTo(a.links, b, "auto");

      const there = yield* a.read(box.environmentId);
      expect(there.isError).toBe(false);
      expect(there.structuredContent).toMatchObject({ environmentId: box.environmentId });
      expect(there.structuredContent).not.toHaveProperty("hostResources");
      expect(there.structuredContent).not.toHaveProperty("placement");
    }),
  ).pipe(Effect.provide(NodeHttpServer.layerTest)),
);
