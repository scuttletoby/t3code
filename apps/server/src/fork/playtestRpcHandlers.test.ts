// Fork (playtest F5): the thread chip's RPC handlers over the real playtest service and its fake
// adapter (PlaytestService.testkit.ts). Each test names the question it answers.
import { assert, describe, it } from "@effect/vitest";
import {
  AuthOrchestrationOperateScope,
  AuthOrchestrationReadScope,
  FORK_PLAYTEST_WS_METHODS as METHODS,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import {
  adapter,
  fixture,
  live,
  makeService,
  OTHER_THREAD,
  ran,
  refusal,
  settingsWith,
  THREAD,
} from "../playtest/PlaytestService.testkit.ts";
import { requiredScopeForRpcMethod } from "../auth/RpcAuthorization.ts";
import * as ForkPlaytestRpcHandlers from "./playtestRpcHandlers.ts";

describe("playtest chip RPCs", () => {
  it.live("status shows the running session's chip, and none for a thread without one", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        const rpc = ForkPlaytestRpcHandlers.make(Option.some(service));

        assert.strictEqual(yield* rpc[METHODS.status]({ threadId: THREAD }), null);
        const { session } = yield* service.start({ threadId: THREAD, target: "dev-1" });
        const chip = yield* rpc[METHODS.status]({ threadId: THREAD });
        assert.deepStrictEqual(chip, {
          id: session.id,
          target: "dev-1",
          note: session.note,
          state: "active",
          startedAt: session.startedAt,
          rounds: 0,
        });
        assert.strictEqual(yield* rpc[METHODS.status]({ threadId: OTHER_THREAD }), null);
      }),
    ),
  );

  it.live("End ends it like the tool's end (release runs), as the person; the chip goes", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        const rpc = ForkPlaytestRpcHandlers.make(Option.some(service));
        yield* service.start({ threadId: THREAD, target: "dev-1" });

        assert.deepStrictEqual(yield* rpc[METHODS.end]({ threadId: THREAD }), {
          outcome: "ended",
        });
        assert.isTrue((yield* ran(dir)).some((line) => line.startsWith("release:")));
        const last = yield* service.status({ threadId: THREAD });
        assert.strictEqual(last?.state, "ended");
        assert.strictEqual(last?.endReason, "person");
        assert.strictEqual(yield* rpc[METHODS.status]({ threadId: THREAD }), null);

        // Pressed again (a second tab, a double click): nothing to end, not an error.
        assert.deepStrictEqual(yield* rpc[METHODS.end]({ threadId: THREAD }), {
          outcome: "nothing-to-end",
        });
      }),
    ),
  );

  it.live("a session ended another way (the agent's end) shows no chip", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        const rpc = ForkPlaytestRpcHandlers.make(Option.some(service));
        yield* service.start({ threadId: THREAD, target: "dev-1" });
        yield* service.end({ threadId: THREAD, reason: "agent" });
        assert.strictEqual(yield* rpc[METHODS.status]({ threadId: THREAD }), null);
      }),
    ),
  );

  it.effect("a server without the playtest service refuses both, as store_unavailable", () =>
    Effect.gen(function* () {
      const rpc = ForkPlaytestRpcHandlers.make(Option.none());
      for (const method of [METHODS.status, METHODS.end] as const) {
        const refused = yield* refusal(rpc[method]({ threadId: THREAD }));
        assert.strictEqual(refused.code, "store_unavailable");
      }
    }),
  );

  it("reading needs the read scope; End needs the operate scope (it runs the release step)", () => {
    assert.strictEqual(requiredScopeForRpcMethod(METHODS.status), AuthOrchestrationReadScope);
    assert.strictEqual(requiredScopeForRpcMethod(METHODS.end), AuthOrchestrationOperateScope);
  });
});
