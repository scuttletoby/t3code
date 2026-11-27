/**
 * Fork (playtest F5): the thread chip's routes. `GET /api/fork/playtest/state?threadId=` (read
 * scope) gives the session that is on; `POST /api/fork/playtest/end` with `{ threadId }` (operate
 * scope) is the person's End: the tool's `end` with the reason `person`.
 */
import {
  AuthOrchestrationOperateScope,
  AuthOrchestrationReadScope,
  PLAYTEST_END_PATH,
  PLAYTEST_STATE_PATH,
  type PlaytestRouteEndAnswer,
  type PlaytestRouteState,
  ThreadId,
} from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import { HttpRouter, HttpServerRequest, HttpServerResponse } from "effect/http";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import { Playtest } from "./PlaytestLayer.ts";
import type { PlaytestService } from "./PlaytestService.ts";

const noStore = { headers: { "cache-control": "no-store" } };
const decodeThreadId = Schema.decodeUnknownOption(ThreadId);
const decodeEndBody = Schema.decodeUnknownOption(Schema.Struct({ threadId: ThreadId }));

type Auth = Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateHttpRequest">;

/** The request's session holds `scope`; else the response to send instead. */
const authorize = Effect.fn("fork.playtest.authorize")(function* (
  auth: Auth,
  scope: typeof AuthOrchestrationReadScope | typeof AuthOrchestrationOperateScope,
) {
  const request = yield* HttpServerRequest.HttpServerRequest;
  const session = yield* auth.authenticateHttpRequest(request).pipe(Effect.option);
  if (Option.isNone(session)) return HttpServerResponse.text("Unauthorized", { status: 401 });
  if (!session.value.scopes.includes(scope)) {
    return HttpServerResponse.text("Forbidden", { status: 403 });
  }
  return null;
});

const badRequest = () => HttpServerResponse.text("Bad Request", { status: 400 });

/** Why a call failed, in one line: a refusal's own message, else a plain one. */
const failureText = (cause: Cause.Cause<unknown>): string => {
  const error = Cause.squash(cause);
  return typeof error === "object" && error !== null && "message" in error
    ? String(error.message)
    : "T3 could not read its playtest sessions now.";
};

/** The thread's session that is on. One that ended, or a failed read, shows no chip. */
export const handleState = Effect.fn("fork.playtest.state")(function* (
  auth: Auth,
  playtest: Pick<PlaytestService, "status">,
) {
  const refused = yield* authorize(auth, AuthOrchestrationReadScope);
  if (refused !== null) return refused;
  const request = yield* HttpServerRequest.HttpServerRequest;
  const url = new URL(request.url, "http://localhost");
  const threadId = decodeThreadId(url.searchParams.get("threadId"));
  if (Option.isNone(threadId)) return badRequest();
  const session = yield* playtest.status({ threadId: threadId.value }).pipe(
    Effect.catchCauseIf(
      (cause) => !Cause.hasInterruptsOnly(cause),
      (cause) =>
        Effect.logWarning("playtest route: the sessions could not be read", {
          threadId: threadId.value,
          cause: Cause.pretty(cause),
        }).pipe(Effect.as(null)),
    ),
  );
  const answer: PlaytestRouteState = {
    session: session === null || session.state === "ended" ? null : session,
  };
  return HttpServerResponse.jsonUnsafe(answer, noStore);
});

/** The person's End: the tool's `end`, reason `person`. A thread with none: `nothing-to-end`. */
export const handleEnd = Effect.fn("fork.playtest.end")(function* (
  auth: Auth,
  playtest: Pick<PlaytestService, "end">,
) {
  const refused = yield* authorize(auth, AuthOrchestrationOperateScope);
  if (refused !== null) return refused;
  const request = yield* HttpServerRequest.HttpServerRequest;
  const body = decodeEndBody(yield* request.json.pipe(Effect.orElseSucceed(() => null)));
  if (Option.isNone(body)) return badRequest();
  const answer = yield* playtest.end({ threadId: body.value.threadId, reason: "person" }).pipe(
    Effect.map((result): PlaytestRouteEndAnswer => result),
    Effect.catchCauseIf(
      (cause) => !Cause.hasInterruptsOnly(cause),
      (cause) =>
        Effect.logWarning("playtest route: the session could not be ended", {
          threadId: body.value.threadId,
          cause: Cause.pretty(cause),
        }).pipe(
          Effect.as<PlaytestRouteEndAnswer>({
            outcome: "unavailable",
            session: null,
            message: failureText(cause),
          }),
        ),
    ),
  );
  return HttpServerResponse.jsonUnsafe(answer, noStore);
});

/** In the server's routes (fork/forkLayers.ts `layerRoutes`). */
export const layer = Layer.unwrap(
  Effect.gen(function* () {
    const auth = yield* EnvironmentAuth.EnvironmentAuth;
    const playtest = yield* Playtest;
    return Layer.mergeAll(
      HttpRouter.add("GET", PLAYTEST_STATE_PATH, handleState(auth, playtest)),
      HttpRouter.add("POST", PLAYTEST_END_PATH, handleEnd(auth, playtest)),
    );
  }),
);
