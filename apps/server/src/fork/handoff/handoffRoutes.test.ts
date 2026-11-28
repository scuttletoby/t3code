// The handoff routes in a real router and HTTP server, with a fake auth and handoff service: who
// may read and press, what a bad request gets, and that a failed read answers off, never a 500.
import { describe, expect, it } from "@effect/vitest";
import * as NodeHttpServer from "@effect/platform-node/NodeHttpServer";
import {
  AuthOrchestrationOperateScope,
  AuthOrchestrationReadScope,
  type ThreadId,
} from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { HttpClient, HttpClientRequest, HttpRouter } from "effect/http";

import type * as EnvironmentAuth from "../../auth/EnvironmentAuth.ts";
import type * as HandoffService from "./HandoffService.ts";
import { HANDOFF_NOW_PATH, HANDOFF_STATE_PATH, handleNow, handleState } from "./handoffRoutes.ts";

/** `authorization: Bearer <scopes, comma separated>`; no header is no session. */
const auth = {
  authenticateHttpRequest: (request: { readonly headers: Record<string, string | undefined> }) => {
    const header = request.headers["authorization"];
    return header === undefined
      ? Effect.fail("no session")
      : Effect.succeed({ scopes: header.replace("Bearer ", "").split(",") });
  },
} as unknown as Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateHttpRequest">;

const READ = { authorization: `Bearer ${AuthOrchestrationReadScope}` };
const OPERATE = {
  authorization: `Bearer ${AuthOrchestrationReadScope},${AuthOrchestrationOperateScope}`,
};

const running: HandoffService.HandoffThreadState = {
  on: true,
  usedTokens: 160_000,
  ask: { kind: "wait", blockers: [{ kind: "run-active" }] },
  waitingOn: [{ kind: "run-active" }],
};

const serve = (failFor: string) =>
  Effect.gen(function* () {
    const asked: Array<ThreadId> = [];
    const service = {
      threadState: (threadId: ThreadId) =>
        threadId === failFor ? Effect.die("read failed") : Effect.succeed(running),
      ask: (threadId: ThreadId) => {
        asked.push(threadId);
        return threadId === failFor
          ? Effect.die("read failed")
          : Effect.succeed({ kind: "started" } as HandoffService.HandoffAskAnswer);
      },
    } as unknown as HandoffService.HandoffService["Service"];
    const routes = Layer.mergeAll(
      HttpRouter.add("GET", HANDOFF_STATE_PATH, handleState(auth, service)),
      HttpRouter.add("POST", HANDOFF_NOW_PATH, handleNow(auth, service)),
    );
    const services = yield* Layer.build(
      HttpRouter.serve(routes, { disableListenLog: true, disableLogger: true }).pipe(
        Layer.provideMerge(NodeHttpServer.layerTest),
      ),
    );
    return { client: Context.get(services, HttpClient.HttpClient), asked };
  });

describe("handoff routes (HO5)", () => {
  it.effect("the state: read scope, a thread id, and off when the thread cannot be read", () =>
    Effect.gen(function* () {
      const { client } = yield* serve("thread-broken");
      const get = (query: string, headers: Record<string, string> = {}) =>
        client.execute(HttpClientRequest.get(`${HANDOFF_STATE_PATH}${query}`, { headers }));

      expect((yield* get("?threadId=thread-a")).status).toBe(401);
      expect((yield* get("?threadId=thread-a", { authorization: "Bearer other" })).status).toBe(
        403,
      );
      expect((yield* get("", READ)).status).toBe(400);

      const ok = yield* get("?threadId=thread-a", READ);
      expect(ok.status).toBe(200);
      expect(ok.headers["cache-control"]).toBe("no-store");
      expect(yield* ok.json).toEqual(running);

      const broken = yield* get("?threadId=thread-broken", READ);
      expect(broken.status).toBe(200);
      expect(yield* broken.json).toEqual({
        on: false,
        usedTokens: null,
        ask: { kind: "off" },
        waitingOn: [],
      });
    }),
  );

  it.effect("the press: operate scope, a JSON body, and off when the thread cannot be read", () =>
    Effect.gen(function* () {
      const { client, asked } = yield* serve("thread-broken");
      const post = (body: string, headers: Record<string, string> = {}) =>
        client.execute(
          HttpClientRequest.post(HANDOFF_NOW_PATH, { headers }).pipe(
            HttpClientRequest.bodyText(body, "application/json"),
          ),
        );
      const thread = (threadId: string) => JSON.stringify({ threadId });

      expect((yield* post(thread("thread-a"))).status).toBe(401);
      // A read-only session may see the state, not press.
      expect((yield* post(thread("thread-a"), READ)).status).toBe(403);
      expect((yield* post("not json", OPERATE)).status).toBe(400);
      expect((yield* post(JSON.stringify({}), OPERATE)).status).toBe(400);
      expect(asked).toEqual([]);

      const ok = yield* post(thread("thread-a"), OPERATE);
      expect(ok.status).toBe(200);
      expect(yield* ok.json).toEqual({ kind: "started" });
      const broken = yield* post(thread("thread-broken"), OPERATE);
      expect(yield* broken.json).toEqual({ kind: "off" });
      expect(asked).toEqual(["thread-a", "thread-broken"]);
    }),
  );
});
