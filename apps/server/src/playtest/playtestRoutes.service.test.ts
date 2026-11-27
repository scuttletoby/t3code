/**
 * Fork (playtest F5): the chip's routes over the real playtest service and its fake adapter (the
 * service's test kit). Is End through the route the tool's end: does `release` run, is the reason
 * `person`, and does the chip go? A second End, and an agent's end, show no chip either.
 */
import { assert, describe, it } from "@effect/vitest";
import * as NodeHttpServer from "@effect/platform-node/NodeHttpServer";
import {
  AuthOrchestrationOperateScope,
  AuthOrchestrationReadScope,
  PLAYTEST_END_PATH,
  PLAYTEST_STATE_PATH,
} from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { HttpClient, HttpClientRequest, HttpRouter } from "effect/http";

import type * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import { handleEnd, handleState } from "./playtestRoutes.ts";
import {
  adapter,
  fixture,
  live,
  makeService,
  OTHER_THREAD,
  ran,
  settingsWith,
  THREAD,
} from "./PlaytestService.testkit.ts";

/** A signed-in session that may read and change threads. */
const auth = {
  authenticateHttpRequest: () =>
    Effect.succeed({ scopes: [AuthOrchestrationReadScope, AuthOrchestrationOperateScope] }),
} as unknown as Pick<EnvironmentAuth.EnvironmentAuth["Service"], "authenticateHttpRequest">;

describe.skipIf(HostProcess.Platform.defaultValue() === "win32")(
  "playtest chip routes over the real service (F5)",
  () => {
    it.live("End through the route releases, ends as person, and the chip goes", () =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          const routes = Layer.mergeAll(
            HttpRouter.add("GET", PLAYTEST_STATE_PATH, handleState(auth, service)),
            HttpRouter.add("POST", PLAYTEST_END_PATH, handleEnd(auth, service)),
          );
          const client = Context.get(
            yield* Layer.build(
              HttpRouter.serve(routes, { disableListenLog: true, disableLogger: true }).pipe(
                Layer.provideMerge(NodeHttpServer.layerTest),
              ),
            ),
            HttpClient.HttpClient,
          );
          const state = (threadId: string) =>
            client
              .execute(HttpClientRequest.get(`${PLAYTEST_STATE_PATH}?threadId=${threadId}`))
              .pipe(Effect.flatMap((response) => response.json));
          const end = (threadId: string) =>
            client
              .execute(
                HttpClientRequest.post(PLAYTEST_END_PATH).pipe(
                  HttpClientRequest.bodyText(JSON.stringify({ threadId }), "application/json"),
                ),
              )
              .pipe(Effect.flatMap((response) => response.json));

          assert.strictEqual(
            (yield* service.start({ threadId: THREAD, target: "dev-1", note: "try the shop" }))
              .outcome,
            "started",
          );
          const on = (yield* state(THREAD)) as { session: Record<string, unknown> };
          assert.deepInclude(on.session, {
            target: "dev-1",
            state: "active",
            note: "try the shop",
            rounds: 0,
            releaseProblem: false,
          });
          assert.notProperty(on.session, "worktree");

          const ended = (yield* end(THREAD)) as { outcome: string; session: { state: string } };
          assert.strictEqual(ended.outcome, "ended");
          assert.strictEqual(ended.session.state, "ended");
          assert.include(yield* ran(dir), "release:0");
          assert.strictEqual((yield* service.status({ threadId: THREAD }))?.endReason, "person");
          assert.deepStrictEqual(yield* state(THREAD), { session: null });
          // Pressed again (another tab, a late click): nothing to end, and no second release.
          assert.deepInclude(yield* end(THREAD), { outcome: "nothing-to-end" });
          assert.lengthOf(
            (yield* ran(dir)).filter((line) => line.startsWith("release:")),
            1,
          );

          // The agent ends its own session with the tool: the chip goes as well.
          yield* service.start({ threadId: OTHER_THREAD, target: "dev-2", note: "" });
          assert.isNotNull(((yield* state(OTHER_THREAD)) as { session: unknown }).session);
          yield* service.end({ threadId: OTHER_THREAD, reason: "agent" });
          assert.deepStrictEqual(yield* state(OTHER_THREAD), { session: null });
        }),
      ),
    );
  },
);
