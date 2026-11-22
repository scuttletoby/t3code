/**
 * Fork: a credential this server cannot check (its session store failed) gets 503, never the 401
 * that ends a linked environment's link. The answer is a defect that is an HTTP response, which
 * Effect's HTTP server sends as it is, so `/mcp` and the linked-thread routes answer it alike.
 */
import * as Effect from "effect/Effect";
import * as HttpServerResponse from "effect/http/HttpServerResponse";

const unavailable = HttpServerResponse.jsonUnsafe(
  {
    error: "temporarily_unavailable",
    error_description: "The server could not check this credential. Try again shortly.",
  },
  { status: 503, headers: { "cache-control": "no-store", "retry-after": "5" } },
);

/** Answers 503 for an internal error met while checking a credential, logged without the token. */
export const answerUnavailable = (error: unknown) =>
  Effect.logWarning("Could not check an MCP client's credential; answered 503.", {
    cause: error,
  }).pipe(Effect.andThen(Effect.die(unavailable)));
