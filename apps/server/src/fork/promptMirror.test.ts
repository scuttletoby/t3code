/**
 * Fork: `GET /api/fork/prompt-mirror` (fork/promptMirror.ts), mounted in a real
 * router with a fake auth: what a parent's open tasks wait on, here and on a linked machine, as
 * read-only cards. Never a secret value; a task that cannot be read is an unknown card, not none.
 */
import { describe, expect, it } from "@effect/vitest";
import * as NodeHttpServer from "@effect/platform-node/NodeHttpServer";
import {
  type EnvironmentId,
  NodeId,
  ProviderInstanceId,
  RunId,
  RuntimeRequestId,
  ThreadId,
  TurnItemId,
} from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";
import * as TestClock from "effect/testing/TestClock";
import { HttpClient, HttpClientRequest, HttpRouter, type HttpServerRequest } from "effect/http";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as PromptMirror from "./promptMirror.ts";
import type * as RemoteTaskPrompts from "./remoteTaskPrompts.ts";

const parentThread = ThreadId.make("thread:mirror-parent");
const localTask = NodeId.make("node:mirror-local");
const remoteTask = NodeId.make("node:mirror-remote");
const doneTask = NodeId.make("node:mirror-done");
const childThread = ThreadId.make("thread:mirror-child");
const childRun = RunId.make("run:mirror-child");
const childNode = NodeId.make("node:mirror-child-node");
const box = "environment:mirror-box" as EnvironmentId;
const boxThread = ThreadId.make("thread:mirror-box-child");

const task = (id: NodeId, fields: Record<string, unknown>) => ({
  id,
  threadId: parentThread,
  origin: "app_owned",
  childThreadId: null,
  providerInstanceId: ProviderInstanceId.make("codex"),
  prompt: `Prompt of ${id}`,
  title: null,
  status: "running",
  result: null,
  ...fields,
});

const SUBAGENTS = [
  task(localTask, { childThreadId: childThread, title: "Build here" }),
  task(remoteTask, {
    remoteChild: { environmentId: box, threadId: boxThread, label: "build box" },
  }),
  // Settled: nothing it asked is waited on any more.
  task(doneTask, { childThreadId: ThreadId.make("thread:mirror-done"), status: "completed" }),
];

const CHILD = {
  thread: { id: childThread, archivedAt: null, deletedAt: null },
  runs: [{ id: childRun, status: "running", delegatedTaskId: localTask }],
  nodes: [{ id: childNode, runId: childRun }],
  runtimeRequests: [
    {
      id: RuntimeRequestId.make("request:mirror-question"),
      nodeId: childNode,
      kind: "user_input",
      status: "pending",
      responseCapability: { type: "live" },
      createdAt: DateTime.makeUnsafe(1_000),
    },
    {
      id: RuntimeRequestId.make("request:mirror-approval"),
      nodeId: childNode,
      kind: "command",
      status: "pending",
      responseCapability: { type: "live" },
      createdAt: DateTime.makeUnsafe(1_000),
    },
  ],
  turnItems: [
    {
      id: TurnItemId.make("item:mirror-secret"),
      type: "secret_request",
      runId: childRun,
      secretStatus: "pending",
      label: "Deploy\ntoken",
      reason: "to ship",
    },
    {
      id: TurnItemId.make("item:mirror-pairing"),
      type: "link_request",
      runId: childRun,
      linkStatus: "pending",
      label: "spare laptop",
      reason: "",
    },
  ],
  contextTransfers: [],
};

/** A browser session that may read orchestration, one that may not, or none. */
const layerFakeAuth = Layer.succeed(EnvironmentAuth.EnvironmentAuth, {
  authenticateHttpRequest: (incoming: HttpServerRequest.HttpServerRequest) => {
    const authorization = incoming.headers.authorization;
    if (authorization === "Bearer reader")
      return Effect.succeed({ scopes: ["orchestration:read"] } as never);
    if (authorization === "Bearer no-scope") return Effect.succeed({ scopes: [] } as never);
    return Effect.fail(new EnvironmentAuth.ServerAuthMissingCredentialError({}));
  },
} as unknown as EnvironmentAuth.EnvironmentAuth["Service"]);

interface World {
  parent: "there" | "missing" | "deleted";
  childFails: boolean;
  there: RemoteTaskPrompts.PromptsRead;
  asks: number;
}

const layerThreads = (world: World) =>
  Layer.mock(ThreadManagement.ThreadManagementService)({
    getThreadShell: () =>
      Effect.succeed(
        world.parent === "missing"
          ? null
          : ({
              deletedAt: world.parent === "deleted" ? "2026-01-01T00:00:00.000Z" : null,
              archivedAt: null,
            } as never),
      ),
    getThreadRecords: ((threadId: ThreadId) =>
      threadId === parentThread
        ? Effect.succeed({ thread: { id: parentThread }, subagents: SUBAGENTS } as never)
        : world.childFails
          ? Effect.die("the database is busy")
          : Effect.succeed(CHILD as never)) as never,
  });

const serve = (world: World) =>
  Effect.gen(function* () {
    const read: RemoteTaskPrompts.ReadRemotePrompts = () =>
      Effect.sync(() => {
        world.asks += 1;
        return world.there;
      });
    const route = Layer.unwrap(
      Effect.gen(function* () {
        const auth = yield* EnvironmentAuth.EnvironmentAuth;
        const threads = yield* ThreadManagement.ThreadManagementService;
        return HttpRouter.add(
          "GET",
          PromptMirror.PROMPT_MIRROR_PATH,
          PromptMirror.handlePromptMirror(auth, threads, PromptMirror.cachedReader(read)),
        );
      }),
    );
    const services = yield* Layer.build(
      HttpRouter.serve(route, { disableListenLog: true, disableLogger: true }).pipe(
        Layer.provideMerge(layerFakeAuth),
        Layer.provideMerge(layerThreads(world)),
        Layer.provideMerge(NodeHttpServer.layerTest),
      ),
    );
    const client = Context.get(services, HttpClient.HttpClient);
    return (token: string | null, query = `?threadId=${parentThread}`) =>
      client.execute(
        HttpClientRequest.get(`${PromptMirror.PROMPT_MIRROR_PATH}${query}`, {
          headers: token === null ? {} : { authorization: `Bearer ${token}` },
        }),
      );
  });

const makeWorld = (): World => ({
  parent: "there",
  childFails: false,
  there: {
    type: "known",
    prompts: {
      found: true,
      questions: [],
      approval: {
        requestId: RuntimeRequestId.make("request:box-approval"),
        kind: "file-change",
        runId: RunId.make("run:box"),
      },
      secrets: [
        { itemId: TurnItemId.make("item:box-secret"), label: "Box key", reason: "to log in" },
      ],
    },
  },
  asks: 0,
});

const decodeMirror = Schema.decodeUnknownSync(PromptMirror.PromptMirror);

describe("the prompt mirror route", () => {
  it.effect("a reader sees each open prompt here and there as a card, no secret value", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const world = makeWorld();
        const get = yield* serve(world);
        const response = yield* get("reader");
        expect(response.status).toBe(200);
        expect(response.headers["cache-control"]).toBe("no-store");
        const mirror = decodeMirror(yield* response.json);
        const here = {
          taskId: localTask,
          task: "Build here",
          machine: null,
          childThreadId: childThread,
        };
        const there = {
          taskId: remoteTask,
          task: `Prompt of ${remoteTask}`,
          machine: { environmentId: box, label: "build box" },
          childThreadId: boxThread,
        };
        const card = (base: object, kind: string, promptId: string, label: string | null) => ({
          ...base,
          promptId,
          kind,
          label,
          status: "open",
          reason: null,
        });
        expect(mirror).toEqual({
          threadId: parentThread,
          entries: [
            card(here, "question", "request:mirror-question", null),
            card(here, "approval", "request:mirror-approval", "command"),
            // One plain line.
            card(here, "secret", "item:mirror-secret", "Deploy token"),
            card(here, "pairing", "item:mirror-pairing", "spare laptop"),
            card(there, "approval", "request:box-approval", "file-change"),
            card(there, "secret", "item:box-secret", "Box key"),
          ],
        });
        // A reason the child wrote is not a status: the card has the label only.
        expect(JSON.stringify(mirror)).not.toContain("to ship");
      }),
    ),
  );

  it.effect("a linked machine is asked once while its answer is fresh, again after", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const world = makeWorld();
        const get = yield* serve(world);
        yield* get("reader");
        yield* get("reader");
        expect(world.asks).toBe(1);
        yield* TestClock.adjust("6 seconds");
        yield* get("reader");
        expect(world.asks).toBe(2);
      }),
    ),
  );

  it.effect("a task that cannot be read is an unknown card, not nothing; not kept", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const world = makeWorld();
        world.childFails = true;
        world.there = { type: "unknown", reason: "unreachable" };
        const get = yield* serve(world);
        const mirror = decodeMirror(yield* (yield* get("reader")).json);
        expect(mirror.entries.map((entry) => [entry.taskId, entry.status, entry.reason])).toEqual([
          [localTask, "unknown", "unreadable"],
          [remoteTask, "unknown", "unreachable"],
        ]);
        // An unknown answer is asked again on the next read.
        yield* get("reader");
        expect(world.asks).toBe(2);
      }),
    ),
  );

  it.effect("a thread gone there waits on nothing", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const world = makeWorld();
        world.there = {
          type: "known",
          prompts: { found: false, questions: [], approval: null, secrets: [] },
        };
        const get = yield* serve(world);
        const mirror = decodeMirror(yield* (yield* get("reader")).json);
        expect(mirror.entries.every((entry) => entry.taskId === localTask)).toBe(true);
      }),
    ),
  );

  it.effect("no session, a session that may not read, no thread id, or no such thread", () =>
    Effect.scoped(
      Effect.gen(function* () {
        const world = makeWorld();
        const get = yield* serve(world);
        expect((yield* get(null)).status).toBe(401);
        expect((yield* get("no-scope")).status).toBe(403);
        expect((yield* get("reader", "")).status).toBe(400);
        world.parent = "missing";
        expect((yield* get("reader")).status).toBe(404);
        world.parent = "deleted";
        expect((yield* get("reader")).status).toBe(404);
        // None of these read a task or asked a machine.
        expect(world.asks).toBe(0);
      }),
    ),
  );
});
