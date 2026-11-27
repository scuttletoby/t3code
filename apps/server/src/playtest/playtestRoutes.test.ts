// The playtest chip's routes (F5) in a real router and HTTP server, with a fake auth and playtest
// service: who may read and end, what a bad request gets, that End is the tool's end with the
// reason `person`, and that an ended session or a failed read shows no chip, never a 500.
import { describe, expect, it } from "@effect/vitest";
import * as NodeHttpServer from "@effect/platform-node/NodeHttpServer";
import {
  AuthOrchestrationOperateScope,
  AuthOrchestrationReadScope,
  PLAYTEST_END_PATH,
  PLAYTEST_STATE_PATH,
  type PlaytestEndReason,
  type PlaytestSessionView,
  PlaytestFailure,
  type ThreadId,
} from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import { HttpClient, HttpClientRequest, HttpRouter } from "effect/http";

import type * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import { chipSessionOf, handleEnd, handleState } from "./playtestRoutes.ts";
import type { PlaytestService } from "./PlaytestService.ts";

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

const session = (threadId: string, state: PlaytestSessionView["state"]) =>
  ({
    id: "pt0000000001",
    threadId,
    projectId: "project-a",
    target: "staging",
    note: "",
    worktree: "/work/tree",
    state,
    startedAt: "2026-11-27T10:00:00.000Z",
    lastActivityAt: "2026-11-27T10:05:00.000Z",
    rounds: 2,
    lastRound: {
      number: 2,
      state: "failed",
      step: "deploy",
      detail: "step output with a path /home/someone/secret",
      startedAt: "2026-11-27T10:04:00.000Z",
    },
    problem: "release: exit 1 with its output",
  }) as unknown as PlaytestSessionView;

const serve = Effect.gen(function* () {
  const ended: Array<{ threadId: ThreadId; reason: PlaytestEndReason }> = [];
  const service = {
    status: ({ threadId }: { threadId: ThreadId }) => {
      if (threadId === "thread-broken") return Effect.die("store gone");
      if (threadId === "thread-none") return Effect.succeed(null);
      if (threadId === "thread-ended") return Effect.succeed(session(threadId, "ended"));
      return Effect.succeed(session(threadId, "active"));
    },
    end: (request: { threadId: ThreadId; reason: PlaytestEndReason }) => {
      ended.push(request);
      if (request.threadId === "thread-broken") {
        return Effect.fail(
          new PlaytestFailure({ code: "store_unavailable", message: "The file is newer." }),
        );
      }
      if (request.threadId === "thread-none") {
        return Effect.succeed({ outcome: "nothing-to-end" as const, session: null });
      }
      return Effect.succeed({
        outcome: "ended" as const,
        session: session(request.threadId, "ended"),
      });
    },
  } as unknown as Pick<PlaytestService, "status" | "end">;
  const routes = Layer.mergeAll(
    HttpRouter.add("GET", PLAYTEST_STATE_PATH, handleState(auth, service)),
    HttpRouter.add("POST", PLAYTEST_END_PATH, handleEnd(auth, service)),
  );
  const services = yield* Layer.build(
    HttpRouter.serve(routes, { disableListenLog: true, disableLogger: true }).pipe(
      Layer.provideMerge(NodeHttpServer.layerTest),
    ),
  );
  return { client: Context.get(services, HttpClient.HttpClient), ended };
});

describe("playtest chip routes (F5)", () => {
  it.effect("the state: read scope, a thread id, and only a session that is on", () =>
    Effect.gen(function* () {
      const { client } = yield* serve;
      const get = (query: string, headers: Record<string, string> = {}) =>
        client.execute(HttpClientRequest.get(`${PLAYTEST_STATE_PATH}${query}`, { headers }));

      expect((yield* get("?threadId=thread-a")).status).toBe(401);
      expect((yield* get("?threadId=thread-a", { authorization: "Bearer other" })).status).toBe(
        403,
      );
      expect((yield* get("", READ)).status).toBe(400);

      const on = yield* get("?threadId=thread-a", READ);
      expect(on.status).toBe(200);
      expect(on.headers["cache-control"]).toBe("no-store");
      const answer = (yield* on.json) as { session: Record<string, unknown> };
      expect(answer).toEqual({ session: chipSessionOf(session("thread-a", "active")) });
      // Only the chip's fields: no worktree, no step output, no problem text.
      expect(Object.keys(answer.session).toSorted()).toEqual(
        ["id", "lastRound", "note", "releaseProblem", "rounds", "state", "target"].toSorted(),
      );
      expect(answer.session.lastRound).toEqual({ number: 2, state: "failed", step: "deploy" });
      expect(JSON.stringify(answer)).not.toMatch(/work\/tree|secret|exit 1/);
      // A problem counts only while the release is what fails (the session is ending).
      expect(answer.session.releaseProblem).toBe(false);
      expect(chipSessionOf(session("thread-a", "ending")).releaseProblem).toBe(true);

      // The last session ended (by the agent, idle, time limit…): no chip.
      expect(yield* (yield* get("?threadId=thread-ended", READ)).json).toEqual({ session: null });
      expect(yield* (yield* get("?threadId=thread-none", READ)).json).toEqual({ session: null });
      const broken = yield* get("?threadId=thread-broken", READ);
      expect(broken.status).toBe(200);
      expect(yield* broken.json).toEqual({ session: null });
    }),
  );

  it.effect("End: operate scope, a JSON body, the tool's end with the reason person", () =>
    Effect.gen(function* () {
      const { client, ended } = yield* serve;
      const post = (body: string, headers: Record<string, string> = {}) =>
        client.execute(
          HttpClientRequest.post(PLAYTEST_END_PATH, { headers }).pipe(
            HttpClientRequest.bodyText(body, "application/json"),
          ),
        );
      const thread = (threadId: string) => JSON.stringify({ threadId });

      expect((yield* post(thread("thread-a"))).status).toBe(401);
      // A read-only session may see the chip, not end the session.
      expect((yield* post(thread("thread-a"), READ)).status).toBe(403);
      expect((yield* post("not json", OPERATE)).status).toBe(400);
      expect((yield* post(JSON.stringify({}), OPERATE)).status).toBe(400);
      expect(ended).toEqual([]);

      const ok = yield* post(thread("thread-a"), OPERATE);
      expect(ok.status).toBe(200);
      expect(yield* ok.json).toEqual({
        outcome: "ended",
        session: chipSessionOf(session("thread-a", "ended")),
      });
      // Pressed again after it ended (or it ended some other way first): nothing to end.
      expect(yield* (yield* post(thread("thread-none"), OPERATE)).json).toEqual({
        outcome: "nothing-to-end",
        session: null,
      });
      const broken = yield* post(thread("thread-broken"), OPERATE);
      expect(broken.status).toBe(200);
      expect(yield* broken.json).toEqual({
        outcome: "unavailable",
        session: null,
        message: "The file is newer.",
      });
      expect(ended).toEqual([
        { threadId: "thread-a", reason: "person" },
        { threadId: "thread-none", reason: "person" },
        { threadId: "thread-broken", reason: "person" },
      ]);
    }),
  );
});
