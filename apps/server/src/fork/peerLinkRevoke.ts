/**
 * Fork: a link the peer turns away ends here too, as an expired one. An older peer answers 401 for
 * its own storage errors as well, so a 401 ends the link only when a fresh MCP session started
 * right after it is refused with 401 too.
 */
import { OrchestratorMcpFailure, type EnvironmentId, type PeerLink } from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Ref from "effect/Ref";
import type * as HttpClientError from "effect/http/HttpClientError";
import type * as HttpClientResponse from "effect/http/HttpClientResponse";
import type * as SqlClient from "effect/sql/SqlClient";

import type * as PeerLinks from "../peer/PeerLinks.ts";

/** PeerMcpClient.ts's MCP version, sent the same way when a fresh session confirms a refusal. */
export const PROTOCOL_VERSION = "2025-06-18";

/** The request that starts a fresh MCP session, as PeerMcpClient's own. */
export const initializeSession = {
  jsonrpc: "2.0",
  id: 0,
  method: "initialize",
  params: {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: {},
    clientInfo: { name: "t3code-peer", version: "1" },
  },
};

const revokedThere = new OrchestratorMcpFailure({
  code: "capability_denied",
  message:
    "The linked environment no longer accepts this link. It may have been revoked there; link it again.",
});

/** PeerMcpClient's answer to a 403 at a session's start. */
const linkedEnvironmentsOffThere = new OrchestratorMcpFailure({
  code: "orchestration_error",
  message:
    "The linked environment has Linked environments turned off. Turn it on in its Settings → Labs.",
});

/** A fresh session showed the link is good, so a 401 on its call is not a revoke. */
const refusedOnFreshSession = new OrchestratorMcpFailure({
  code: "orchestration_error",
  message:
    "The linked environment accepted this link but refused the call (HTTP 401). Try again shortly.",
});

type Forget = (environmentId: EnvironmentId) => Effect.Effect<void>;

/**
 * PeerMcpClient's `forget`, per PeerLinks service (keyed by its `recordRevoked`), so a link ended
 * by a thread read or a client pairing (peerLinkRevokeCheck.ts) drops the session ids an agent's
 * calls kept for it too, as one an agent's call ends does.
 */
const forgetters = new WeakMap<object, Array<Forget>>();

/**
 * PeerLinks' side: `record` ends a link the peer turned away, and `notEnded` keeps a renewal
 * that answers a call made before the refusal from moving that link's end out again.
 */
export const makeLinkRevokes = (sql: SqlClient.SqlClient) => {
  // The ended link's linked_at, per environment. A renewal in flight needs it only while this
  // process runs: a restart ends every call that could still bring one back.
  const ended = new Map<EnvironmentId, string>();
  const forgets: Array<Forget> = [];
  const record = Effect.fn("PeerLinks.recordRevoked")(
    function* (link: PeerLink) {
      const linkedAt = DateTime.formatIso(link.linkedAt);
      ended.set(link.environmentId, linkedAt);
      yield* Effect.forEach(forgets, (forget) => forget(link.environmentId), { discard: true });
      const now = DateTime.formatIso(yield* DateTime.now);
      // Only the link the refused call resolved: one made again since keeps its own end.
      yield* sql`
        UPDATE peer_environment_links SET expires_at = ${now}
        WHERE environment_id = ${link.environmentId}
          AND linked_at = ${linkedAt}
          AND expires_at > ${now}
      `;
    },
    Effect.ignoreCause({ log: true }),
  );
  const notEnded = (environmentId: EnvironmentId) => {
    const linkedAt = ended.get(environmentId);
    return linkedAt === undefined ? sql.literal("") : sql`AND linked_at <> ${linkedAt}`;
  };
  forgetters.set(record, forgets);
  return { record, notEnded };
};

type Post = (
  url: string,
  token: string,
  body: unknown,
  headers: Record<string, string>,
) => Effect.Effect<HttpClientResponse.HttpClientResponse, HttpClientError.HttpClientError>;

/** PeerMcpClient's side: what it does after a 401, with its own request and session cache. */
export const makeRefusals = (deps: {
  readonly links: PeerLinks.PeerLinks["Service"];
  readonly post: Post;
  /** PeerMcpClient's sessions, keyed `${environmentId} ${url}`. */
  readonly sessions: Ref.Ref<ReadonlyMap<string, string>>;
  readonly recordLinkExpiry: (
    link: PeerLink,
    headers: Readonly<Record<string, string>>,
  ) => Effect.Effect<void>;
}) => {
  const { links, post, sessions, recordLinkExpiry } = deps;
  const forget = (environmentId: EnvironmentId) =>
    Ref.update(
      sessions,
      (current) => new Map([...current].filter(([key]) => !key.startsWith(`${environmentId} `))),
    );
  forgetters.get(links.recordRevoked)?.push(forget);

  /**
   * The peer refused to start a session: starts one more right away. Refused again with 401, the
   * link is over here too; otherwise the session it starts is used.
   */
  const reopen = Effect.fnUntraced(function* (
    link: PeerLink,
    url: string,
    token: string,
    headers: Record<string, string>,
  ) {
    yield* forget(link.environmentId);
    const initialized = yield* post(url, token, initializeSession, headers);
    if (initialized.status === 401) {
      yield* links.recordRevoked(link);
      return yield* revokedThere;
    }
    if (initialized.status === 403) return yield* linkedEnvironmentsOffThere;
    const sessionId = initialized.headers["mcp-session-id"];
    if (initialized.status !== 200 || sessionId === undefined) {
      return yield* new OrchestratorMcpFailure({
        code: "orchestration_error",
        message: `The linked environment refused to start an MCP session (HTTP ${initialized.status}).`,
      });
    }
    yield* recordLinkExpiry(link, initialized.headers);
    yield* post(
      url,
      token,
      { jsonrpc: "2.0", method: "notifications/initialized" },
      { ...headers, "mcp-session-id": sessionId, "mcp-protocol-version": PROTOCOL_VERSION },
    );
    return sessionId;
  });

  /**
   * A call on a kept session was refused: makes the call once more on a fresh session (`open`,
   * which ends the link if the peer refuses that too). A 401 stops a request before
   * any tool runs there, so the call is never made twice.
   */
  const retry = Effect.fnUntraced(function* <E, R>(
    sessionKey: string,
    open: Effect.Effect<string, E, R>,
    callOnce: (
      sessionId: string,
    ) => Effect.Effect<HttpClientResponse.HttpClientResponse, HttpClientError.HttpClientError>,
  ) {
    // A refusal there drops the kept session (`reopen`); one it accepts replaces it.
    const sessionId = yield* open;
    yield* Ref.update(sessions, (current) => new Map(current).set(sessionKey, sessionId));
    const response = yield* callOnce(sessionId);
    if (response.status === 401) return yield* refusedOnFreshSession;
    return response;
  });

  return { reopen, retry };
};
