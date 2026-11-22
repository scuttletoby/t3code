/**
 * Fork: a link the peer turns away ends here, and only then. The box is the real peer on a
 * socket (PeerLinks.testkit.ts); a proxy in front of it plays an older box that answers 401 once
 * for a storage error of its own.
 */
import { NodeHttpServer } from "@effect/platform-node";
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { expect, it } from "@effect/vitest";
import {
  OrchestratorMcpFailure,
  PEER_LINK_THREAD_STREAM_ROUTE,
  type PeerLink,
} from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";
import { Tool, Toolkit } from "effect/ai";
import {
  FetchHttpClient,
  HttpBody,
  HttpClient,
  HttpClientRequest,
  HttpRouter,
  HttpServer,
  HttpServerRequest,
  HttpServerResponse,
} from "effect/http";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as McpOAuth from "../auth/McpOAuth.ts";
import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";
import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import * as McpHttpServer from "../mcp/McpHttpServer.ts";
import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as McpSessionRegistry from "../mcp/McpSessionRegistry.ts";
import * as McpToolAccess from "../mcp/McpToolAccess.ts";
import * as OrchestrationEventStore from "../persistence/OrchestrationEventStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as LinkThreadHttp from "../peer/linkThreadHttp.ts";
import * as PeerLinks from "../peer/PeerLinks.ts";
import {
  descriptorOf,
  layerLinkingEnvironment,
  linkTo,
  servePeer,
  type ServedPeer,
} from "../peer/PeerLinks.testkit.ts";
import * as PeerMcpClient from "../peer/PeerMcpClient.ts";
import * as Sqlite from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettings from "../serverSettings.ts";

const laptop = descriptorOf("environment-laptop", "Laptop");
const box = descriptorOf("environment-box", "Box");

const NoTools = Toolkit.make(
  Tool.make("noop", { success: Schema.Struct({}), failure: OrchestratorMcpFailure }),
);
const boxTools = McpHttpServer.toolkitRegistration(
  NoTools,
  McpToolAccess.toLayer(NoTools, { noop: McpToolAccess.reads(() => Effect.succeed({})) }),
);
const serveBox = servePeer(box, boxTools);

/** The laptop, which links to the box. */
const makeLaptop = layerLinkingEnvironment(laptop).pipe(
  Layer.build,
  Effect.map((context) => ({
    links: Context.get(context, PeerLinks.PeerLinks),
    peers: Context.get(context, PeerMcpClient.PeerMcpClient),
    sql: Context.get(context, SqlClient.SqlClient),
  })),
);
type Laptop = Effect.Success<typeof makeLaptop>;

const noop = (a: Laptop) =>
  a.peers.call({
    environmentId: box.environmentId,
    tool: "noop",
    arguments: {},
    limits: { runtimeMode: "full-access", interactionMode: "default" },
    success: Schema.Struct({}),
  });
const statuses = (a: Laptop) =>
  a.links.list.pipe(Effect.map((links) => links.map((link) => link.status)));

/** One `/mcp` request the proxy saw: its JSON-RPC method, session, and the status it answered. */
interface Seen {
  readonly method: string;
  readonly session: string | undefined;
  readonly status: number;
}

/**
 * A proxy in front of `target` on a socket of its own. It answers 401 for the next `/mcp`
 * requests whose JSON-RPC methods `refuse` lists (once each, in order), as an older box does for
 * a storage error; with `down` set it answers every `/mcp` request 502, and with `downAfter` set
 * it goes down once it answered the last refusal.
 */
const serveProxy = (target: string) =>
  Effect.gen(function* () {
    const refuse = yield* Ref.make<ReadonlyArray<string>>([]);
    const down = yield* Ref.make(false);
    const downAfter = yield* Ref.make(false);
    const seen = yield* Ref.make<ReadonlyArray<Seen>>([]);
    const server = yield* Layer.build(Layer.fresh(NodeHttpServer.layerTest));
    const handle = (request: HttpServerRequest.HttpServerRequest) =>
      Effect.gen(function* () {
        const body = yield* request.text;
        const isMcp = request.url.startsWith("/mcp");
        const method = isMcp ? ((JSON.parse(body) as { method?: string }).method ?? "") : "";
        const note = (status: number) =>
          isMcp
            ? Ref.update(seen, (all) => [
                ...all,
                { method, session: request.headers["mcp-session-id"], status },
              ])
            : Effect.void;
        if (isMcp && (yield* Ref.get(down))) {
          yield* note(502);
          return HttpServerResponse.empty({ status: 502 });
        }
        const queued = yield* Ref.get(refuse);
        if (isMcp && queued[0] === method) {
          yield* Ref.set(refuse, queued.slice(1));
          if (queued.length === 1 && (yield* Ref.get(downAfter))) yield* Ref.set(down, true);
          yield* note(401);
          return HttpServerResponse.jsonUnsafe({ error: "invalid_token" }, { status: 401 });
        }
        const headers = Object.fromEntries(
          Object.entries(request.headers).filter(
            ([name]) => !["host", "content-length", "connection"].includes(name),
          ),
        );
        const forwarded = yield* HttpClient.execute(
          HttpClientRequest.make(request.method as "GET" | "POST")(`${target}${request.url}`).pipe(
            HttpClientRequest.setHeaders(headers),
            body.length > 0
              ? HttpClientRequest.setBody(
                  HttpBody.text(body, request.headers["content-type"] ?? "application/json"),
                )
              : (same) => same,
          ),
        );
        const text = yield* forwarded.text;
        yield* note(forwarded.status);
        const kept = Object.fromEntries(
          ["mcp-session-id", McpInvocationContext.LINK_EXPIRES_AT_HEADER].flatMap((name) => {
            const value = forwarded.headers[name];
            return value === undefined ? [] : [[name, value]];
          }),
        );
        return HttpServerResponse.text(text, {
          status: forwarded.status,
          headers: kept,
          contentType: forwarded.headers["content-type"] ?? "application/json",
        });
      }).pipe(Effect.provide(FetchHttpClient.layer), Effect.orDie);
    const url = yield* Effect.gen(function* () {
      yield* HttpServer.serve(Effect.flatMap(HttpServerRequest.HttpServerRequest, handle)).pipe(
        Layer.build,
      );
      const address = (yield* HttpServer.HttpServer).address;
      if (address._tag === "UnixPathAddress") return yield* Effect.die("expected a TCP address");
      return `http://127.0.0.1:${address.port}`;
    }).pipe(Effect.provideContext(server));
    return { url, refuse, down, downAfter, seen };
  });

/** Links the laptop to the box, then points the stored link at a proxy in front of it. */
const linkThroughProxy = (a: Laptop, b: ServedPeer) =>
  Effect.gen(function* () {
    yield* linkTo(a.links, b);
    const proxy = yield* serveProxy(b.url);
    yield* a.sql`
      UPDATE peer_environment_links SET urls_json = ${JSON.stringify([proxy.url])}
      WHERE environment_id = ${box.environmentId}
    `;
    return proxy;
  });

const storedLink = (a: Laptop) =>
  a.links.get(box.environmentId).pipe(Effect.map(Option.getOrThrow));

it.effect("a refused call is made again on a fresh session, and the link stays", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBox;
      const a = yield* makeLaptop;
      const proxy = yield* linkThroughProxy(a, b);
      yield* noop(a);

      // An older box answers 401 for its own storage error: the fresh session it then accepts
      // shows the link is good, so the call goes through and nothing ends.
      yield* Ref.set(proxy.refuse, ["tools/call"]);
      yield* Ref.set(proxy.seen, []);
      expect(yield* noop(a)).toEqual({});
      expect(
        (yield* Ref.get(proxy.seen)).map(({ method, status }) => `${method} ${status}`),
      ).toEqual([
        "tools/call 401",
        "initialize 200",
        "notifications/initialized 202",
        "tools/call 200",
      ]);
      expect(yield* statuses(a)).toEqual(["reachable"]);

      // The fresh session is the one kept for the next call.
      const [, opened] = yield* Ref.get(proxy.seen);
      yield* Ref.set(proxy.seen, []);
      yield* noop(a);
      const [next] = yield* Ref.get(proxy.seen);
      expect(next?.method).toBe("tools/call");
      expect(next?.session).toBeDefined();
      expect(opened?.status).toBe(200);
    }),
  ).pipe(Effect.provide(NodeHttpServer.layerTest)),
);

it.effect("a session refused once at its start is started again, and the link stays", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBox;
      const a = yield* makeLaptop;
      const proxy = yield* linkThroughProxy(a, b);

      // No session yet (as after a restart here): the first initialize is the one refused.
      yield* Ref.set(proxy.refuse, ["initialize"]);
      expect(yield* noop(a)).toEqual({});
      expect(
        (yield* Ref.get(proxy.seen)).map(({ method, status }) => `${method} ${status}`),
      ).toEqual([
        "initialize 401",
        "initialize 200",
        "notifications/initialized 202",
        "tools/call 200",
      ]);
      expect(yield* statuses(a)).toEqual(["reachable"]);
    }),
  ).pipe(Effect.provide(NodeHttpServer.layerTest)),
);

it.effect("a link the box revoked ends here, and its kept session goes with it", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBox;
      const a = yield* makeLaptop;
      const proxy = yield* linkThroughProxy(a, b);
      yield* noop(a);

      const [session] = yield* b.linkedSessions;
      yield* b.auth.revokeSession(session!.sessionId);
      const refused = yield* noop(a).pipe(Effect.flip);
      expect(refused.code).toBe("capability_denied");
      expect(refused.message).toContain("revoked there");
      // Turned away twice in a row, fresh sessions included: the link is over here too.
      expect(yield* statuses(a)).toEqual(["expired"]);
      expect(yield* a.links.names).toEqual([]);

      // Linked again with a new pairing code, the first call starts a session of its own
      // instead of sending the revoked link's session id.
      yield* linkTo(a.links, b);
      yield* a.sql`
        UPDATE peer_environment_links SET urls_json = ${JSON.stringify([proxy.url])}
        WHERE environment_id = ${box.environmentId}
      `;
      yield* Ref.set(proxy.seen, []);
      expect(yield* noop(a)).toEqual({});
      const [first] = yield* Ref.get(proxy.seen);
      expect(first).toMatchObject({ method: "initialize", session: undefined, status: 200 });
      expect(yield* statuses(a)).toEqual(["reachable"]);
    }),
  ).pipe(Effect.provide(NodeHttpServer.layerTest)),
);

it.effect("a refusal whose check cannot reach the box leaves the link as it was", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBox;
      const a = yield* makeLaptop;
      const proxy = yield* linkThroughProxy(a, b);
      yield* noop(a);
      const before = yield* storedLink(a);

      // Stopped half-way: the call is refused, and the fresh session that would confirm it gets
      // no answer. Nothing proves the link is gone, so it stays.
      yield* Ref.set(proxy.refuse, ["tools/call"]);
      yield* Ref.set(proxy.downAfter, true);
      const failed = yield* noop(a).pipe(Effect.flip);
      expect(failed.code).toBe("orchestration_error");
      const after = yield* storedLink(a);
      expect(DateTime.formatIso(after.expiresAt)).toBe(DateTime.formatIso(before.expiresAt));

      // Runs again: once the box answers, the next call simply works.
      yield* Ref.set(proxy.downAfter, false);
      yield* Ref.set(proxy.down, false);
      expect(yield* noop(a)).toEqual({});
      expect(yield* statuses(a)).toEqual(["reachable"]);
    }),
  ).pipe(Effect.provide(NodeHttpServer.layerTest)),
);

/** A stored link the laptop made at `linkedAt`, written as PeerLinks does. */
const insertLink = (a: Laptop, linkedAt: DateTime.Utc, expiresAt: DateTime.Utc, fenced = 1) =>
  a.sql`
    INSERT INTO peer_environment_links (
      environment_id, secret_name, label, urls_json, access, linked_at, expires_at,
      last_reached_at, last_error, fenced
    ) VALUES (
      ${box.environmentId}, 'peer-link-box', 'Box', '[]', 'auto',
      ${DateTime.formatIso(linkedAt)}, ${DateTime.formatIso(expiresAt)}, NULL, NULL, ${fenced}
    )
    ON CONFLICT (environment_id) DO UPDATE SET
      linked_at = excluded.linked_at, expires_at = excluded.expires_at, fenced = excluded.fenced
  `;
const expiresAt = (a: Laptop) =>
  storedLink(a).pipe(Effect.map((link) => DateTime.formatIso(link.expiresAt)));

it.effect("a refusal of a token replaced since leaves the new link standing", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const a = yield* makeLaptop;
      const now = yield* DateTime.now;
      yield* insertLink(a, DateTime.add(now, { minutes: 1 }), DateTime.add(now, { days: 30 }));
      const relinked = yield* storedLink(a);
      // A call that resolved the link before it was made again hears the old token refused.
      const old: PeerLink = { ...relinked, linkedAt: now };
      yield* a.links.recordRevoked(old);
      expect(yield* a.links.names).toEqual([{ environmentId: box.environmentId, label: "Box" }]);

      // Runs again: a second refusal of the new link ends it once, and a third changes nothing.
      yield* a.links.recordRevoked(relinked);
      const ended = yield* expiresAt(a);
      yield* a.links.recordRevoked(relinked);
      expect(yield* expiresAt(a)).toBe(ended);
      expect(yield* statuses(a)).toEqual(["expired"]);
      expect(yield* a.links.names).toEqual([]);
    }),
  ),
);

it.effect("a renewal answering a call made before the refusal never brings the link back", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const a = yield* makeLaptop;
      const now = yield* DateTime.now;
      yield* insertLink(a, now, DateTime.add(now, { days: 30 }));
      const link = yield* storedLink(a);
      yield* a.links.recordRevoked(link);
      const ended = yield* expiresAt(a);

      // A call that succeeded just before the revoke brings its renewal header after it.
      yield* a.links.recordExpiry(box.environmentId, DateTime.add(now, { days: 31 }));
      expect(yield* expiresAt(a)).toBe(ended);
      expect(yield* statuses(a)).toEqual(["expired"]);

      // Linked again (a new linked_at), its renewals count again.
      const relinkedAt = DateTime.add(now, { minutes: 1 });
      yield* insertLink(a, relinkedAt, DateTime.add(now, { days: 30 }));
      const renewed = DateTime.add(now, { days: 32 });
      yield* a.links.recordExpiry(box.environmentId, renewed);
      expect(yield* expiresAt(a)).toBe(DateTime.formatIso(renewed));
    }),
  ),
);

it.effect("an old unfenced row stays expired through a refusal and a renewal", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const a = yield* makeLaptop;
      const now = yield* DateTime.now;
      // Stored before links were fenced: it already reads as expired and asks for a new code.
      yield* insertLink(a, now, DateTime.add(now, { days: 30 }), 0);
      expect(yield* statuses(a)).toEqual(["expired"]);
      yield* a.links.recordRevoked(yield* storedLink(a));
      yield* a.links.recordExpiry(box.environmentId, DateTime.add(now, { days: 40 }));
      expect(yield* statuses(a)).toEqual(["expired"]);
      expect(yield* a.links.names).toEqual([]);
    }),
  ),
);

// The box's side: its own session store failing is not a revoke, so it answers 503, not 401.

/** The box with its database at hand, serving `/mcp` and the linked-thread routes. */
const serveBoxWithDatabase = Effect.gen(function* () {
  const authContext = yield* Layer.build(
    EnvironmentAuth.layer.pipe(
      Layer.provide(ManagedPolicy.layerTest()),
      Layer.provideMerge(ServerSettings.layerTest({ linkedEnvironmentsEnabled: true })),
      Layer.provideMerge(Sqlite.layerMemory),
      Layer.provideMerge(ServerSecretStore.layer),
      Layer.provideMerge(ServerEnvironment.layerIdentity),
      Layer.provideMerge(ServerConfig.layerTest(process.cwd(), { prefix: "t3-fork-revoke-box-" })),
      Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
      Layer.fresh,
    ),
  );
  const routes = Layer.mergeAll(
    McpHttpServer.layerMcpTransport(boxTools).pipe(
      Layer.provide(
        Layer.mock(McpSessionRegistry.McpSessionRegistry)({ resolve: () => Effect.undefined }),
      ),
      Layer.provide(McpOAuth.layerMcpClientAuthenticator),
    ),
    LinkThreadHttp.layer,
  ).pipe(
    Layer.provide(
      Layer.succeed(ServerEnvironment.ServerEnvironment, {
        getEnvironmentId: Effect.succeed(box.environmentId),
        getDescriptor: Effect.succeed(box),
      }),
    ),
    Layer.provide(Layer.succeedContext(authContext)),
  );
  // The linked-thread routes refuse before they read a thread, so they need none.
  const noThreads = Layer.merge(
    Layer.mock(ThreadManagement.ThreadManagementService)({}),
    Layer.mock(OrchestrationEventStore.OrchestrationEventStore)({}),
  );
  yield* HttpRouter.serve(routes, { disableListenLog: true, disableLogger: true }).pipe(
    Layer.provide(Layer.succeedContext(authContext)),
    Layer.provide(noThreads),
    Layer.fresh,
    Layer.build,
  );
  const address = (yield* HttpServer.HttpServer).address;
  if (address._tag === "UnixPathAddress") return yield* Effect.die("expected a TCP address");
  const issued = yield* Context.get(authContext, EnvironmentAuth.EnvironmentAuth)
    .issueMcpClientSession({
      label: "T3 Code · Laptop",
      access: "auto",
      peerLink: true,
      client: { deviceType: "desktop", os: "macOS", browser: "T3 Code", ipAddress: "127.0.0.1" },
    })
    .pipe(Effect.orDie);
  return {
    url: `http://127.0.0.1:${address.port}`,
    token: issued.token,
    sql: Context.get(authContext, SqlClient.SqlClient),
  };
});

it.effect("the box answers 503, not 401, while it cannot read its sessions", () =>
  Effect.scoped(
    Effect.gen(function* () {
      const b = yield* serveBoxWithDatabase;
      const initialize = HttpClient.execute(
        HttpClientRequest.post(`${b.url}/mcp`).pipe(
          HttpClientRequest.setHeaders({
            accept: "application/json, text/event-stream",
            authorization: `Bearer ${b.token}`,
          }),
          HttpClientRequest.setBody(
            HttpBody.text(
              JSON.stringify({
                jsonrpc: "2.0",
                id: 0,
                method: "initialize",
                params: {
                  protocolVersion: "2025-06-18",
                  capabilities: {},
                  clientInfo: { name: "fork-revoke-test", version: "1" },
                },
              }),
              "application/json",
            ),
          ),
        ),
      ).pipe(Effect.map((response) => response.status));
      const stream = HttpClient.execute(
        HttpClientRequest.get(`${b.url}${PEER_LINK_THREAD_STREAM_ROUTE}?thread=thread:box`).pipe(
          HttpClientRequest.setHeaders({ authorization: `Bearer ${b.token}` }),
        ),
      ).pipe(Effect.map((response) => response.status));
      expect(yield* initialize).toBe(200);

      yield* b.sql`ALTER TABLE auth_sessions RENAME TO auth_sessions_away`;
      expect(yield* initialize).toBe(503);
      expect(yield* stream).toBe(503);

      // The token itself was never in doubt: once the store reads again, it is accepted.
      yield* b.sql`ALTER TABLE auth_sessions_away RENAME TO auth_sessions`;
      expect(yield* initialize).toBe(200);
      // A token the box does not know is still refused with 401.
      const unknown = yield* HttpClient.execute(
        HttpClientRequest.get(`${b.url}${PEER_LINK_THREAD_STREAM_ROUTE}?thread=thread:box`).pipe(
          HttpClientRequest.setHeaders({ authorization: "Bearer not-a-token" }),
        ),
      );
      expect(unknown.status).toBe(401);
    }).pipe(Effect.provide(FetchHttpClient.layer)),
  ).pipe(Effect.provide(NodeHttpServer.layerTest)),
);
