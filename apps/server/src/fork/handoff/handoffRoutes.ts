/**
 * Fork (HO5): the web app's handoff routes. `GET /api/fork/handoff/state?threadId=` gives a
 * thread's handoff for the thread menu and the "handoff waiting on" chip (read scope); `POST
 * /api/fork/handoff/now` with `{ threadId }` is a person's "Hand off now" (operate scope). Both
 * answer JSON; any other caller gets 401 or 403, and a thread the service cannot read reads as off.
 */
import {
  AuthOrchestrationOperateScope,
  AuthOrchestrationReadScope,
  HANDOFF_NOW_PATH,
  HANDOFF_STATE_PATH,
  type HandoffRouteAnswer,
  type HandoffRouteState,
  ThreadId,
} from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import { HttpRouter, HttpServerRequest, HttpServerResponse } from "effect/http";

import * as EnvironmentAuth from "../../auth/EnvironmentAuth.ts";
import * as HandoffService from "./HandoffService.ts";

export { HANDOFF_NOW_PATH, HANDOFF_STATE_PATH };

const noStore = { headers: { "cache-control": "no-store" } };
const decodeThreadId = Schema.decodeUnknownOption(ThreadId);
const decodeNowBody = Schema.decodeUnknownOption(Schema.Struct({ threadId: ThreadId }));

/** The request's session holds `scope`; else the response to send instead. */
const authorize = Effect.fn("fork.handoff.authorize")(function* (
  auth: Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateHttpRequest">,
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

/** A thread the service cannot read (gone, or a failed read) answers as off, never a 500. */
const offOnFailure = <A, E>(effect: Effect.Effect<A, E>, off: A, threadId: ThreadId) =>
  effect.pipe(
    Effect.catchCauseIf(
      (cause) => !Cause.hasInterruptsOnly(cause),
      (cause) =>
        Effect.logWarning("handoff route: the thread could not be read", {
          threadId,
          cause: Cause.pretty(cause),
        }).pipe(Effect.as(off)),
    ),
  );

const badRequest = () => HttpServerResponse.text("Bad Request", { status: 400 });

/** The two routes with the server's auth and handoff service passed in (tests give mocks). */
export const handleState = Effect.fn("fork.handoff.state")(function* (
  auth: Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateHttpRequest">,
  service: Pick<HandoffService.HandoffService["Service"], "threadState">,
) {
  const refused = yield* authorize(auth, AuthOrchestrationReadScope);
  if (refused !== null) return refused;
  const request = yield* HttpServerRequest.HttpServerRequest;
  const url = new URL(request.url, "http://localhost");
  const threadId = decodeThreadId(url.searchParams.get("threadId"));
  if (Option.isNone(threadId)) return badRequest();
  const state = yield* offOnFailure(
    service.threadState(threadId.value),
    {
      on: false,
      usedTokens: null,
      ask: { kind: "off" },
      waitingOn: [],
    } as HandoffService.HandoffThreadState,
    threadId.value,
  );
  return HttpServerResponse.jsonUnsafe(state satisfies HandoffRouteState, noStore);
});

export const handleNow = Effect.fn("fork.handoff.now")(function* (
  auth: Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateHttpRequest">,
  service: Pick<HandoffService.HandoffService["Service"], "ask">,
) {
  const refused = yield* authorize(auth, AuthOrchestrationOperateScope);
  if (refused !== null) return refused;
  const request = yield* HttpServerRequest.HttpServerRequest;
  const body = decodeNowBody(yield* request.json.pipe(Effect.orElseSucceed(() => null)));
  if (Option.isNone(body)) return badRequest();
  const answer = yield* offOnFailure(
    service.ask(body.value.threadId),
    { kind: "off" } as HandoffService.HandoffAskAnswer,
    body.value.threadId,
  );
  return HttpServerResponse.jsonUnsafe(answer satisfies HandoffRouteAnswer, noStore);
});

/** In the server's routes (fork/forkLayers.ts `layerRoutes`). */
export const layer = Layer.unwrap(
  Effect.gen(function* () {
    const auth = yield* EnvironmentAuth.EnvironmentAuth;
    const service = yield* HandoffService.HandoffService;
    return Layer.mergeAll(
      HttpRouter.add("GET", HANDOFF_STATE_PATH, handleState(auth, service)),
      HttpRouter.add("POST", HANDOFF_NOW_PATH, handleNow(auth, service)),
    );
  }),
);
