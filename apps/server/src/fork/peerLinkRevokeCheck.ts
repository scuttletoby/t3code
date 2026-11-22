/**
 * Fork: a link the peer turns away on a linked-thread read or a client pairing ends here too, as one
 * an agent's call meets does (peerLinkRevoke.ts). A single 401 is no proof, since an older peer
 * answers 401 for its own storage errors as well: the link ends only when a fresh MCP session,
 * started right after at the same address with the same token, is refused with 401 too.
 */
import * as Effect from "effect/Effect";
import * as Predicate from "effect/Predicate";
import * as HttpClient from "effect/http/HttpClient";
import * as HttpClientRequest from "effect/http/HttpClientRequest";

import * as PeerLinks from "../peer/PeerLinks.ts";
import * as ForkLinkRevoke from "./peerLinkRevoke.ts";

/**
 * After a 401 for `resolved`: ends that link (only the `linked_at` the refused request resolved)
 * when a fresh session is refused too. A check with any other answer, or none, leaves it.
 */
export const confirm = Effect.fn("PeerLinks.confirmRevoked")(
  function* (
    links: PeerLinks.PeerLinks["Service"],
    httpClient: HttpClient.HttpClient,
    resolved: PeerLinks.ResolvedPeerLink,
  ) {
    const client = HttpClient.withScope(httpClient);
    const withToken = (request: HttpClientRequest.HttpClientRequest) =>
      HttpClientRequest.bearerToken(request, resolved.token);
    // The session served only this check; the peer would keep it in memory. It is ended when
    // the check stops, however it stops (a timeout, the caller going away), with its own bound.
    const end = (sessionId: string) =>
      httpClient
        .execute(
          withToken(
            HttpClientRequest.delete(`${resolved.url}/mcp`).pipe(
              HttpClientRequest.setHeaders({
                "mcp-session-id": sessionId,
                "mcp-protocol-version": ForkLinkRevoke.PROTOCOL_VERSION,
              }),
            ),
          ),
        )
        .pipe(Effect.timeout(PeerLinks.probeTimeout(resolved.url)), Effect.ignore);
    const initialized = yield* Effect.uninterruptibleMask((restore) =>
      restore(
        client.execute(
          withToken(
            HttpClientRequest.post(`${resolved.url}/mcp`).pipe(
              HttpClientRequest.setHeader("accept", "application/json, text/event-stream"),
              HttpClientRequest.bodyJsonUnsafe(ForkLinkRevoke.initializeSession),
            ),
          ),
        ),
      ).pipe(
        Effect.tap((response) => {
          const sessionId = response.headers["mcp-session-id"];
          return sessionId === undefined ? Effect.void : Effect.addFinalizer(() => end(sessionId));
        }),
      ),
    );
    if (initialized.status === 401) return yield* links.recordRevoked(resolved.link);
  },
  // Nothing is logged: a failed request carries the token.
  (effect, _links, _httpClient, resolved) =>
    effect.pipe(Effect.timeout(PeerLinks.probeTimeout(resolved.url)), Effect.scoped, Effect.ignore),
);

/** `confirm` for a client pairing's refusal, which is a 401 when the peer answered Unauthorized. */
export const onUnauthorized =
  (
    links: PeerLinks.PeerLinks["Service"],
    httpClient: HttpClient.HttpClient,
    resolved: PeerLinks.ResolvedPeerLink,
  ) =>
  (error: unknown) =>
    Predicate.isTagged(error, "EnvironmentHttpUnauthorizedError")
      ? confirm(links, httpClient, resolved)
      : Effect.void;
