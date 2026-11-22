/**
 * Fork: a link the box revoked ends here on a linked-thread read and a client pairing too, as on an
 * agent's call. The box is its real routes, run in this process: the laptop's fetch hands each
 * request to them, and can answer one in the box's place first, as an older box does for a storage
 * error of its own.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeHttpPlatform from "@effect/platform-node/NodeHttpPlatform";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { expect, it } from "@effect/vitest";
import {
  AuthStandardClientScopes,
  EnvironmentHttpApi,
  type OrchestrationV2StoredEvent,
  OrchestratorMcpFailure,
  PEER_LINK_THREAD_STREAM_ROUTE,
  ThreadId,
} from "@t3tools/contracts";
import type * as Cause from "effect/Cause";
import * as Context from "effect/Context";
import * as Deferred from "effect/Deferred";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Queue from "effect/Queue";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";
import * as SqlClient from "effect/sql/SqlClient";
import { Tool, Toolkit } from "effect/ai";
import { Etag, FetchHttpClient, HttpClient, HttpEffect, HttpRouter } from "effect/http";
import * as HttpApi from "effect/http-api/HttpApi";
import * as HttpApiBuilder from "effect/http-api/HttpApiBuilder";
import { TestClock } from "effect/testing";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as AuthHttp from "../auth/http.ts";
import * as McpOAuth from "../auth/McpOAuth.ts";
import * as McpOAuthHttp from "../auth/mcpOAuthHttp.ts";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as CloudLink from "../cloud/CloudLink.ts";
import * as ServerConfig from "../config.ts";
import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import * as ServerHttp from "../http.ts";
import * as McpHttpServer from "../mcp/McpHttpServer.ts";
import * as McpSessionRegistry from "../mcp/McpSessionRegistry.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import { idleThreadProjection, liveThreadShell } from "../mcp/McpToolAccess.testkit.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import * as Sqlite from "../persistence/Sqlite.ts";
import * as ForkRevokeCheck from "./peerLinkRevokeCheck.ts";
import * as PeerHttp from "../peer/http.ts";
import * as LinkThreadHttp from "../peer/linkThreadHttp.ts";
import * as PeerClientPairing from "../peer/PeerClientPairing.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import {
  descriptorOf,
  layerEnvironmentAuth,
  layerLinkingEnvironment,
} from "../peer/PeerLinks.testkit.ts";
import * as PeerMcpClient from "../peer/PeerMcpClient.ts";
import * as PeerThreadRelay from "../peer/PeerThreadRelay.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettings from "../serverSettings.ts";

const laptop = descriptorOf("environment-laptop", "Laptop");
const box = descriptorOf("environment-box", "Box", {
  linkThreadStream: true,
  linkClientPairing: true,
  linkPairingForLink: true,
});
/** Never dialed: the laptop's fetch hands every request to the box's routes in this process. */
const BOX_URL = "http://127.0.0.1:4999";
const PAIRING_ROUTE = "/api/peer-link/client-pairing";
const boxThread = ThreadId.make("thread:box");
const read = { environmentId: box.environmentId, threadId: boxThread };

const NoTools = Toolkit.make(
  Tool.make("noop", { success: Schema.Struct({}), failure: OrchestratorMcpFailure }),
);
const boxTools = McpHttpServer.toolkitRegistration(
  NoTools,
  McpToolAccess.toLayer(NoTools, { noop: McpToolAccess.reads(() => Effect.succeed({})) }),
);

class BoxApi extends HttpApi.make("environment")
  .add(EnvironmentHttpApi.groups.metadata)
  .add(EnvironmentHttpApi.groups.mcpOAuth)
  .add(EnvironmentHttpApi.groups.peerLink) {}

/** A request the laptop's fetch holds: `reached` as it arrives, passed on once `release` resolves. */
interface Hold {
  readonly reached: () => void;
  readonly release: Promise<void>;
}

/**
 * What the laptop's fetch answers in the box's place, once: `rpc` is a `/mcp` JSON-RPC method.
 * With `hold` it answers nothing itself: the request waits, then goes to the box, or is aborted.
 */
type Answer = {
  readonly path: string;
  readonly method?: string;
  readonly rpc?: string;
} & (
  | { readonly status: number; readonly error?: string; readonly hold?: undefined }
  | { readonly hold: Hold }
);

/** A hold the test reaches and releases; one never released is a box that never answers. */
const held = () => {
  const reached = Promise.withResolvers<void>();
  const release = Promise.withResolvers<void>();
  return {
    hold: { reached: () => reached.resolve(), release: release.promise },
    reached: Effect.promise(() => reached.promise),
    release: Effect.sync(() => release.resolve()),
  };
};

/** The box: its descriptor, MCP OAuth, `/mcp`, client pairing and one thread to read. */
const serveBox = Effect.gen(function* () {
  const authContext = yield* Layer.build(
    EnvironmentAuth.layer.pipe(
      Layer.provide(ManagedPolicy.layerTest()),
      Layer.provideMerge(ServerSettings.layerTest({ linkedEnvironmentsEnabled: true })),
      Layer.provideMerge(Sqlite.layerMemory),
      Layer.provideMerge(ServerSecretStore.layer),
      Layer.provideMerge(ServerEnvironment.layerIdentity),
      Layer.provideMerge(ServerConfig.layerTest(process.cwd(), { prefix: "t3-fork-reads-box-" })),
      Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
      Layer.fresh,
    ),
  );
  const live = yield* Queue.unbounded<OrchestrationV2StoredEvent, Cause.Done>();
  const shell = { ...liveThreadShell(boxThread, { activeRunId: null }), title: "On the box" };
  const threads = Layer.mergeAll(
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: (threadId) => Effect.succeed(threadId === boxThread ? shell : null),
      ensureLegacyTranscript: () => Effect.void,
      getThreadSnapshotWindow: () =>
        Effect.succeed({ snapshotSequence: 7, projection: idleThreadProjection(shell) } as never),
      streamStoredEventsFrom: () => Stream.fromQueue(live),
    }),
    Layer.mock(OrchestrationEventStore.OrchestrationEventStore)({}),
    Layer.succeed(ServerEnvironment.ServerEnvironment, {
      getEnvironmentId: Effect.succeed(box.environmentId),
      getDescriptor: Effect.succeed(box),
    }),
  );
  // What its routes and each request they serve read, as the server provides it.
  const served = yield* Layer.build(
    threads.pipe(Layer.provideMerge(Layer.succeedContext(authContext))),
  );
  const routes = Layer.mergeAll(
    HttpApiBuilder.layer(BoxApi).pipe(
      Layer.provide(
        McpOAuthHttp.layer.pipe(
          Layer.provide(McpOAuth.layer),
          Layer.provide(
            Layer.mock(CloudLink.CloudLink)({
              linkState: () =>
                Effect.succeed({
                  linked: false,
                  cloudUserId: null,
                  relayUrl: null,
                  relayIssuer: null,
                  publishAgentActivity: false,
                }),
            }),
          ),
        ),
      ),
      Layer.provide(PeerHttp.layer.pipe(Layer.provide(PeerClientPairing.layerIssuer))),
      Layer.provide(ServerHttp.layerServerEnvironmentHttpApi),
      Layer.provide(AuthHttp.layerAuthenticatedAuth),
    ),
    McpHttpServer.layerMcpTransport(boxTools).pipe(
      Layer.provide(
        Layer.mock(McpSessionRegistry.McpSessionRegistry)({ resolve: () => Effect.undefined }),
      ),
      Layer.provide(McpOAuth.layerMcpClientAuthenticator),
    ),
    LinkThreadHttp.layer,
  ).pipe(
    Layer.provide(Layer.succeedContext(served)),
    Layer.provide(Layer.mergeAll(NodeHttpPlatform.layer, Etag.layerWeak, NodeServices.layer)),
  );
  // Built here, so the box keeps the test's clock.
  const app = (yield* HttpRouter.toHttpEffect(routes)).pipe(Effect.provide(served));
  const handler = HttpEffect.toWebHandlerWith<never, Effect.Services<typeof app>>(
    yield* Effect.context<never>(),
  )(app);
  const answers: Array<Answer> = [];
  const seen: Array<string> = [];
  const fetch: typeof globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    request.headers.set("host", new URL(request.url).host);
    const path = new URL(request.url).pathname;
    const rpc =
      path === "/mcp" && request.method === "POST"
        ? ((JSON.parse(await request.clone().text()) as { method?: string }).method ?? "")
        : undefined;
    const label = [request.method, path, rpc].filter((part) => part !== undefined).join(" ");
    const index = answers.findIndex(
      (answer) =>
        answer.path === path &&
        (answer.method === undefined || answer.method === request.method) &&
        (answer.rpc === undefined || answer.rpc === rpc),
    );
    const [answer] = index === -1 ? [] : answers.splice(index, 1);
    if (answer?.hold !== undefined) {
      answer.hold.reached();
      const aborted = new Promise<"aborted">((resolve) =>
        request.signal.addEventListener("abort", () => {
          seen.push(`${label} aborted`);
          resolve("aborted");
        }),
      );
      if ((await Promise.race([answer.hold.release, aborted])) === "aborted") {
        throw new DOMException("The request was aborted.", "AbortError");
      }
    } else if (answer !== undefined) {
      seen.push(`${label} ${answer.status}`);
      // The pairing route's refusals are typed errors; the others name their reason.
      const body =
        path === PAIRING_ROUTE
          ? {
              _tag: "EnvironmentHttpUnauthorizedError",
              message: "Only a linked environment's session is accepted here.",
            }
          : { error: answer.error ?? "invalid_token" };
      return Response.json(body, { status: answer.status });
    }
    const response = await handler(request);
    seen.push(`${label} ${response.status}`);
    return response;
  };
  const auth = Context.get(authContext, EnvironmentAuth.EnvironmentAuth);
  return {
    auth,
    settings: Context.get(authContext, ServerSettings.ServerSettingsService),
    sql: Context.get(authContext, SqlClient.SqlClient),
    fetch,
    /** Answered in the box's place, each once, before the box sees the request. */
    answers,
    /** Each request the box (or an answer in its place) answered: method, path, RPC, status. */
    seen,
    revokeLink: Effect.gen(function* () {
      const sessions = yield* auth.listSessions();
      const linked = sessions.filter((s) => s.client.label?.startsWith("T3 Code · "));
      for (const session of linked) yield* auth.revokeSession(session.sessionId);
    }),
  };
});
type Box = Effect.Success<typeof serveBox>;

/** The laptop, linked to the box at full access: its links, agent calls, relay and pairing. */
const makeLaptop = (b: Box) =>
  Effect.gen(function* () {
    const linking = yield* Layer.build(layerLinkingEnvironment(laptop));
    const authContext = yield* Layer.build(layerEnvironmentAuth("t3-fork-reads-laptop-"));
    const auth = Context.get(authContext, EnvironmentAuth.EnvironmentAuth);
    const services = yield* Layer.mergeAll(PeerThreadRelay.layer, PeerClientPairing.layer).pipe(
      Layer.provide(Layer.succeedContext(linking)),
      Layer.provide(Layer.succeed(EnvironmentAuth.EnvironmentAuth, auth)),
      Layer.provide(FetchHttpClient.layer),
      Layer.build,
    );
    const links = Context.get(linking, PeerLinks.PeerLinks);
    const pairing = yield* b.auth.issuePairingCredential();
    yield* links.link({ url: BOX_URL, pairingCode: pairing.credential, access: "full-access" });
    const phone = yield* auth.issueSession({ scopes: AuthStandardClientScopes, label: "Phone" });
    const peers = Context.get(linking, PeerMcpClient.PeerMcpClient);
    const relay = Context.get(services, PeerThreadRelay.PeerThreadRelay);
    const pairings = Context.get(services, PeerClientPairing.PeerClientPairing);
    return {
      links,
      statuses: links.list.pipe(Effect.map((all) => all.map((link) => link.status))),
      /** The reason a read of the box's thread fails. */
      readFails: relay.subscribe(read).pipe(
        Stream.runHead,
        Effect.flip,
        Effect.map((error) => error.reason),
      ),
      pair: pairings.request({ environmentId: box.environmentId }, phone),
      noop: peers.call({
        environmentId: box.environmentId,
        tool: "noop",
        arguments: {},
        limits: { runtimeMode: "full-access", interactionMode: "default" },
        success: Schema.Struct({}),
      }),
    };
  });

/** Runs `test` with a box and a laptop linked to it, the laptop's fetch reaching the box. */
const withLink = <A, E>(
  test: (b: Box, a: Effect.Success<ReturnType<typeof makeLaptop>>) => Effect.Effect<A, E>,
) =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBox;
      return yield* Effect.gen(function* () {
        const a = yield* makeLaptop(b);
        b.seen.length = 0;
        return yield* test(b, a);
      }).pipe(Effect.provideService(FetchHttpClient.Fetch, b.fetch));
    }),
  );

const STREAM = `GET ${PEER_LINK_THREAD_STREAM_ROUTE}`;
const PAIRING = `POST ${PAIRING_ROUTE}`;

it.effect("a thread read the box turns away ends the link here, and the next asks no more", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      yield* b.revokeLink;
      expect(yield* a.readFails).toBe("rejected");
      // Turned away twice in a row, a fresh MCP session included: the link is over here too.
      expect(b.seen).toEqual([
        "GET /.well-known/t3/environment 200",
        `${STREAM} 401`,
        "POST /mcp initialize 401",
      ]);
      expect(yield* a.statuses).toEqual(["expired"]);

      // Runs again: the ended link sends the box nothing more.
      b.seen.length = 0;
      expect(yield* a.readFails).toBe("expired");
      expect(b.seen).toEqual([]);
    }),
  ),
);

it.effect("a client pairing the box turns away ends the link here", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      yield* b.revokeLink;
      expect(yield* a.pair).toMatchObject({ type: "unsupported", reason: "refused" });
      expect(b.seen.slice(1)).toEqual([`${PAIRING} 401`, "POST /mcp initialize 401"]);
      expect(yield* a.statuses).toEqual(["expired"]);

      const ended = yield* a.pair.pipe(Effect.flip);
      expect(ended.reason).toBe("expired");
    }),
  ),
);

it.effect("a 403 on a read or a pairing leaves the link, and asks for no fresh session", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      yield* b.settings.updateSettings({ linkedEnvironmentsEnabled: false });
      expect(yield* a.readFails).toBe("rejected");
      expect(yield* a.pair).toMatchObject({ type: "unsupported", reason: "refused" });
      b.answers.push({
        path: PEER_LINK_THREAD_STREAM_ROUTE,
        status: 403,
        error: "not_a_link_session",
      });
      expect(yield* a.readFails).toBe("rejected");
      expect(b.seen.filter((line) => line.includes("/mcp"))).toEqual([]);
      yield* b.settings.updateSettings({ linkedEnvironmentsEnabled: true });
      expect(yield* a.statuses).toEqual(["reachable"]);
    }),
  ),
);

it.effect("a single 401 the fresh session then disproves leaves the link standing", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      // An older box answers 401 for a storage error of its own; the fresh session it then
      // accepts shows the link is good. The session served only the check and is ended there.
      b.answers.push({ path: PEER_LINK_THREAD_STREAM_ROUTE, status: 401 });
      expect(yield* a.readFails).toBe("rejected");
      expect(b.seen.slice(1)).toEqual([
        `${STREAM} 401`,
        "POST /mcp initialize 200",
        "DELETE /mcp 204",
      ]);

      b.seen.length = 0;
      b.answers.push({ path: PAIRING_ROUTE, status: 401 });
      expect(yield* a.pair).toMatchObject({ type: "unsupported", reason: "refused" });
      expect(b.seen.slice(1)).toEqual([
        `${PAIRING} 401`,
        "POST /mcp initialize 200",
        "DELETE /mcp 204",
      ]);
      expect(yield* a.statuses).toEqual(["reachable"]);
      expect(yield* a.pair).toMatchObject({ type: "credential" });
    }),
  ),
);

it.effect("a 401 whose fresh session gets no answer leaves the link as it was", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      const before = Option.getOrThrow(yield* a.links.get(box.environmentId)).expiresAt;
      // Stopped half-way: the read is refused and the check that would confirm it fails.
      b.answers.push({ path: PEER_LINK_THREAD_STREAM_ROUTE, status: 401 });
      b.answers.push({ path: "/mcp", rpc: "initialize", status: 502 });
      expect(yield* a.readFails).toBe("rejected");
      const after = Option.getOrThrow(yield* a.links.get(box.environmentId)).expiresAt;
      expect(DateTime.formatIso(after)).toBe(DateTime.formatIso(before));
      expect(yield* a.statuses).toEqual(["reachable"]);
    }),
  ),
);

it.effect("a 401 whose fresh session never answers stops the check at its bound", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      // The box refuses the read, then never answers the check: the read still ends.
      const silent = held();
      b.answers.push({ path: PEER_LINK_THREAD_STREAM_ROUTE, status: 401 });
      b.answers.push({ path: "/mcp", rpc: "initialize", hold: silent.hold });
      const reading = yield* a.readFails.pipe(Effect.forkChild);
      yield* silent.reached;
      yield* TestClock.adjust(PeerLinks.probeTimeout(BOX_URL));
      expect(yield* Fiber.join(reading)).toBe("rejected");
      expect(b.seen.slice(1)).toEqual([`${STREAM} 401`, "POST /mcp initialize aborted"]);
      expect(yield* a.statuses).toEqual(["reachable"]);
    }),
  ),
);

it.effect("a check stopped after the box accepted its fresh session still ends that session", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      // The read goes away (its thread view closed) while the check ends the session it started.
      const ending = held();
      b.answers.push({ path: PEER_LINK_THREAD_STREAM_ROUTE, status: 401 });
      b.answers.push({ path: "/mcp", method: "DELETE", hold: ending.hold });
      const reading = yield* a.readFails.pipe(Effect.forkChild);
      yield* ending.reached;
      reading.interruptUnsafe();
      yield* ending.release;
      expect(Exit.hasInterrupts(yield* Fiber.await(reading))).toBe(true);
      expect(b.seen.slice(1)).toEqual([
        `${STREAM} 401`,
        "POST /mcp initialize 200",
        "DELETE /mcp 204",
      ]);
      expect(yield* a.statuses).toEqual(["reachable"]);
    }),
  ),
);

it.effect("a link a read ends drops the session an agent's call kept for it", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      expect(yield* a.noop).toEqual({});
      yield* b.revokeLink;
      expect(yield* a.readFails).toBe("rejected");
      expect(yield* a.statuses).toEqual(["expired"]);
      yield* TestClock.adjust(Duration.minutes(1));
      const pairing = yield* b.auth.issuePairingCredential();
      yield* a.links.link({ url: BOX_URL, pairingCode: pairing.credential, access: "full-access" });

      // The new link's first call starts its own session, not the ended link's.
      b.seen.length = 0;
      expect(yield* a.noop).toEqual({});
      expect(b.seen.filter((line) => line.includes("/mcp"))[0]).toBe("POST /mcp initialize 200");
    }),
  ),
);

it.effect("a refusal of a link made again since leaves the new link standing", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      // A read resolved the link, then the box revoked it and the laptop linked again.
      const old = yield* a.links.resolve(box.environmentId);
      yield* b.revokeLink;
      yield* TestClock.adjust(Duration.minutes(1));
      const pairing = yield* b.auth.issuePairingCredential();
      yield* a.links.link({ url: BOX_URL, pairingCode: pairing.credential, access: "full-access" });

      // The old token's check is refused: it ends only the link that read resolved.
      yield* ForkRevokeCheck.confirm(a.links, yield* HttpClient.HttpClient, old);
      expect(b.seen.at(-1)).toBe("POST /mcp initialize 401");
      expect(yield* a.statuses).toEqual(["reachable"]);
      expect(yield* a.pair).toMatchObject({ type: "credential" });
    }).pipe(Effect.provide(FetchHttpClient.layer)),
  ),
);

it.effect("a fresh session the box answers 403 says Linked environments is off there", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      b.answers.push({ path: "/mcp", rpc: "initialize", status: 401 });
      b.answers.push({
        path: "/mcp",
        rpc: "initialize",
        status: 403,
        error: "linked_environments_off",
      });
      const refused = yield* a.noop.pipe(Effect.flip);
      expect(refused.message).toContain("Linked environments turned off");
      expect(yield* a.statuses).toEqual(["reachable"]);
    }),
  ),
);

it.effect("a call refused again on a fresh session the box accepted does not say revoked", () =>
  withLink((b, a) =>
    Effect.gen(function* () {
      expect(yield* a.noop).toEqual({});
      b.answers.push({ path: "/mcp", rpc: "tools/call", status: 401 });
      b.answers.push({ path: "/mcp", rpc: "tools/call", status: 401 });
      const refused = yield* a.noop.pipe(Effect.flip);
      expect(refused.code).toBe("orchestration_error");
      expect(refused.message).not.toContain("revoked");
      expect(yield* a.statuses).toEqual(["reachable"]);
      expect(yield* a.noop).toEqual({});
    }),
  ),
);

// The box's side: an open thread stream whose recheck cannot read sessions ends cleanly.
it.effect("a stream whose 30 s recheck meets a store failure ends cleanly", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBox;
      const issued = yield* b.auth.issueMcpClientSession({
        label: "T3 Code · Laptop",
        access: "auto",
        peerLink: true,
        client: { deviceType: "desktop", os: "macOS", browser: "T3 Code", ipAddress: "127.0.0.1" },
      });
      const response = yield* Effect.promise(() =>
        b.fetch(`${BOX_URL}${PEER_LINK_THREAD_STREAM_ROUTE}?thread=${boxThread}`, {
          headers: { authorization: `Bearer ${issued.token}` },
        }),
      );
      expect(response.status).toBe(200);
      const started = yield* Deferred.make<void>();
      const drained = yield* Stream.fromReadableStream({
        evaluate: () => response.body!,
        onError: (cause) => cause,
      }).pipe(
        Stream.tap(() => Deferred.succeed(started, undefined)),
        Stream.runDrain,
        Effect.exit,
        Effect.forkChild,
      );
      // Its first heartbeat arrived: the stream runs, and with it the recheck.
      yield* Deferred.await(started);
      yield* b.sql`ALTER TABLE auth_sessions RENAME TO auth_sessions_away`;
      yield* TestClock.adjust(Duration.seconds(30));
      expect(Exit.isSuccess(yield* Fiber.join(drained))).toBe(true);
    }),
  ),
);
