// Fork: the slot filler's route. The start's steps with a fake world (wake once, ask again, never two
// children), and both routes mounted in the real router with a fake session check, thread store and MCP server.
import { describe, expect, it } from "@effect/vitest";
import * as NodeHttpServer from "@effect/platform-node/NodeHttpServer";
import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  type OrchestratorMcpDelegateTaskInput,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Scope from "effect/Scope";
import { HttpClient, HttpClientRequest, HttpRouter, HttpServerRequest } from "effect/http";

import * as EnvironmentAuth from "../auth/EnvironmentAuth.ts";
import * as ServerConfig from "../config.ts";
import * as ServerHttp from "../http.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import * as FillerDelegate from "./fillerDelegate.ts";

const parentId = ThreadId.make("thread:filler-parent");
const parent = { threadId: parentId, providerInstanceId: ProviderInstanceId.make("claudeAgent") };
const task = { task: "Read the brief.", title: "writer one" } as OrchestratorMcpDelegateTaskInput;
const input = { parentThreadId: parentId, requestId: "item-1:1", task };

/** A fake world: `answers` in order (the last repeats), what it was asked, the wakes sent. */
const world = (
  answers: ReadonlyArray<FillerDelegate.StartAnswer>,
  options: { readonly parentGone?: boolean; readonly wake?: "sent" | "no_slot" } = {},
) => {
  const asked: Array<OrchestratorMcpDelegateTaskInput> = [];
  const wakes: Array<string> = [];
  const value: FillerDelegate.FillerWorld = {
    parent: () => Effect.succeed(options.parentGone === true ? null : parent),
    delegate: (_parent, given) =>
      Effect.sync(() => {
        asked.push(given);
        return answers[Math.min(asked.length - 1, answers.length - 1)]!;
      }),
    wake: (_parent, requestId, text) =>
      Effect.sync(() => {
        wakes.push(`${requestId}: ${text}`);
        return options.wake ?? "sent";
      }),
    pause: Effect.void,
  };
  return { value, asked, wakes };
};
const started = { type: "started", taskId: "task-1", childThreadId: "thread:child" } as const;
const idle = { type: "refused", code: "parent_not_active", message: "no run" } as const;
const full = { type: "refused", code: "slots_full", message: "10 of 10" } as const;

describe("a slot filler start", () => {
  it.effect("starts at once as the parent, async, under the caller's request id; no wake", () =>
    Effect.gen(function* () {
      const w = world([started]);
      expect(yield* FillerDelegate.runFillerStart(w.value, input)).toEqual(started);
      expect(w.asked).toEqual([{ ...task, mode: "async", clientRequestId: "item-1:1" }]);
      expect(w.wakes).toEqual([]);
    }),
  );

  it.effect("an idle parent is woken once, then asked again until its run shows", () =>
    Effect.gen(function* () {
      const w = world([idle, idle, idle, started]);
      expect(yield* FillerDelegate.runFillerStart(w.value, input)).toEqual(started);
      expect(w.asked).toHaveLength(4);
      // Every try is the same request: the server returns one task for it, never two.
      expect(new Set(w.asked.map((a) => a.clientRequestId))).toEqual(new Set(["item-1:1"]));
      expect(w.wakes).toEqual([
        'item-1:1: [slot filler] Starting "writer one" as your child; no action needed unless it fails.',
      ]);
    }),
  );

  it.effect("a parent that never gets a run: refused after the tries, one wake", () =>
    Effect.gen(function* () {
      const w = world([idle]);
      const answer = yield* FillerDelegate.runFillerStart(w.value, { ...input, wake: "  hello  " });
      expect(answer).toEqual(idle);
      expect(w.asked).toHaveLength(1 + FillerDelegate.PARENT_RUN_TRIES);
      expect(w.wakes).toEqual(["item-1:1: hello"]);
    }),
  );

  it.effect("no slot to wake the parent: refused slots_full, nothing asked again", () =>
    Effect.gen(function* () {
      const w = world([idle], { wake: "no_slot" });
      const answer = yield* FillerDelegate.runFillerStart(w.value, input);
      expect(answer.type === "refused" && answer.code).toBe("slots_full");
      expect(w.asked).toHaveLength(1);
    }),
  );

  it.effect("another refusal (slots, usage, policy) is passed on as is, with no wake", () =>
    Effect.gen(function* () {
      const w = world([full]);
      expect(yield* FillerDelegate.runFillerStart(w.value, input)).toEqual(full);
      expect(w.wakes).toEqual([]);
    }),
  );

  it.effect("a gone parent: nothing asked", () =>
    Effect.gen(function* () {
      const w = world([started], { parentGone: true });
      const answer = yield* FillerDelegate.runFillerStart(w.value, input);
      expect(answer.type === "refused" && answer.code).toBe("parent_missing");
      expect(w.asked).toEqual([]);
    }),
  );
});

describe("the children list", () => {
  it("keeps the parent's own children, live or not, with their machine", () => {
    const base = {
      title: "t",
      childThreadId: null,
      providerInstanceId: "claudeAgent",
      model: "m",
    };
    expect(
      FillerDelegate.childrenOf([
        { ...base, id: "a", origin: "app_owned", status: "running" },
        { ...base, id: "b", origin: "provider_native", status: "running" },
        {
          ...base,
          id: "c",
          origin: "app_owned",
          status: "completed",
          remoteChild: { environmentId: "env-far" },
        },
      ]).map((c) => [c.taskId, c.live, c.environmentId]),
    ).toEqual([
      ["a", true, null],
      ["c", false, "env-far"],
    ]);
  });
});

/** Sessions: "operate" has both scopes, "reader" only read; anything else is no session. */
const layerFakeAuth = Layer.succeed(EnvironmentAuth.EnvironmentAuth, {
  authenticateHttpRequest: (incoming: HttpServerRequest.HttpServerRequest) => {
    const authorization = incoming.headers.authorization;
    if (authorization === "Bearer operate")
      return Effect.succeed({ scopes: ["orchestration:read", "orchestration:operate"] } as never);
    if (authorization === "Bearer reader")
      return Effect.succeed({ scopes: ["orchestration:read"] } as never);
    return Effect.fail(new EnvironmentAuth.ServerAuthMissingCredentialError({}));
  },
} as unknown as EnvironmentAuth.EnvironmentAuth["Service"]);

interface Store {
  /** null: the parent is gone; else its run. */
  shell: { activeRunId: string | null } | null;
  dispatched: Array<unknown>;
  calls: Array<Record<string, unknown>>;
}

const layerStore = (store: Store) =>
  Layer.mergeAll(
    Layer.mock(ThreadManagement.ThreadManagementService)({
      getThreadShell: () =>
        Effect.succeed(
          store.shell === null
            ? null
            : ({
                deletedAt: null,
                archivedAt: null,
                providerInstanceId: "claudeAgent",
                activeRunId: store.shell.activeRunId,
              } as never),
        ),
      getThreadRecords: () =>
        Effect.succeed({
          subagents: [
            {
              id: "task-live",
              origin: "app_owned",
              title: "w",
              status: "running",
              childThreadId: "thread:c",
              providerInstanceId: "claudeAgent",
              model: "claude-opus-5-5",
            },
          ],
        } as never),
      dispatch: (command: unknown) =>
        Effect.sync(() => {
          store.dispatched.push(command);
          // The wake starts the parent's run.
          store.shell = { activeRunId: "run:woken" };
          return {} as never;
        }),
    }),
    Layer.mock(CommandReceiptStore.CommandReceiptStoreV2)({
      getByCommandId: () => Effect.succeedNone,
    }),
  );

/** The MCP server's delegate_task: refused while the parent has no run, else one task per request id. */
const fakeMcp = (store: Store): FillerDelegate.FillerMcp => ({
  callTool: (request) =>
    Effect.sync(() => {
      store.calls.push(request.arguments);
      if (store.shell?.activeRunId == null) {
        return {
          isError: true,
          content: [{ type: "text", text: "no run" }],
          structuredContent: { code: "parent_not_active", message: "no run" },
        } as never;
      }
      return {
        content: [],
        structuredContent: {
          taskId: `task:${String(request.arguments.clientRequestId)}`,
          childThreadId: null,
        },
      } as never;
    }),
});

const serve = (store: Store) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const config = yield* ServerConfig.ServerConfig;
    const staticDir = yield* fs.makeTempDirectoryScoped({ prefix: "t3-filler-" });
    yield* fs.writeFileString(path.join(staticDir, "index.html"), "<!doctype html><p>app");
    yield* FillerDelegate.installFillerDelegateServer(fakeMcp(store), undefined);
    const layerApp = Layer.merge(
      FillerDelegate.layerRoutes.pipe(Layer.provide(layerStore(store))),
      ServerHttp.layerStaticAndDevRoute,
    );
    const services = yield* Layer.build(
      HttpRouter.serve(layerApp, { disableListenLog: true, disableLogger: true }).pipe(
        Layer.provideMerge(layerFakeAuth),
        Layer.provideMerge(ServerSettingsService.ServerSettingsService.layerTest()),
        Layer.provideMerge(ManagedPolicy.layerTest()),
        Layer.provideMerge(ServerConfig.layer({ ...config, staticDir })),
        Layer.provideMerge(NodeHttpServer.layerTest),
      ),
    );
    const client = Context.get(services, HttpClient.HttpClient);
    const path0 = FillerDelegate.FILLER_DELEGATE_PATH;
    return {
      post: (headers: Record<string, string>, body: unknown) =>
        client
          .execute(
            HttpClientRequest.post(path0, { headers }).pipe(
              HttpClientRequest.bodyText(typeof body === "string" ? body : JSON.stringify(body)),
            ),
          )
          .pipe(Effect.flatMap((r) => Effect.map(r.text, (text) => ({ status: r.status, text })))),
      get: (headers: Record<string, string>, query = `?parentThreadId=${parentId}`) =>
        client
          .execute(HttpClientRequest.get(`${path0}${query}`, { headers }))
          .pipe(Effect.flatMap((r) => Effect.map(r.text, (text) => ({ status: r.status, text })))),
    };
  });

const provided = <A, E>(
  effect: Effect.Effect<
    A,
    E,
    ServerConfig.ServerConfig | FileSystem.FileSystem | Path.Path | Scope.Scope
  >,
) =>
  effect.pipe(
    Effect.scoped,
    Effect.provide(
      ServerConfig.layerTest(process.cwd(), { prefix: "t3-filler-config-" }).pipe(
        Layer.provideMerge(NodeServices.layer),
      ),
    ),
  );

const operate = { authorization: "Bearer operate" };

describe("the slot filler routes in the server's router", () => {
  // Live: the wait for the woken parent's run is a real 2 s pause.
  it.live("starts a child for an idle parent: one wake, then the task; again: the same task", () =>
    provided(
      Effect.gen(function* () {
        const store: Store = { shell: { activeRunId: null }, dispatched: [], calls: [] };
        const server = yield* serve(store);
        const first = yield* server.post(operate, input);
        expect(first.status).toBe(200);
        expect(JSON.parse(first.text)).toEqual({
          state: "started",
          taskId: "task:item-1:1",
          childThreadId: null,
        });
        expect(store.dispatched).toHaveLength(1);
        expect(store.dispatched[0]).toMatchObject({
          type: "message.dispatch",
          commandId: "slot-filler:wake:item-1:1",
          threadId: parentId,
        });
        expect(
          store.calls.every((c) => c.mode === "async" && c.clientRequestId === "item-1:1"),
        ).toBe(true);
        // A repeat (the caller crashed before its record): the same task, no second wake.
        const again = yield* server.post(operate, input);
        expect(JSON.parse(again.text).taskId).toBe("task:item-1:1");
        expect(store.dispatched).toHaveLength(1);
      }),
    ),
  );

  it.effect(
    "refuses a bad body, a gone parent, a read-only session, no session and a browser",
    () =>
      provided(
        Effect.gen(function* () {
          const store: Store = { shell: { activeRunId: "run:1" }, dispatched: [], calls: [] };
          const server = yield* serve(store);
          expect((yield* server.post(operate, "{not json")).status).toBe(400);
          expect((yield* server.post(operate, { ...input, requestId: "bad id/x" })).status).toBe(
            400,
          );
          expect((yield* server.post({ authorization: "Bearer reader" }, input)).status).toBe(403);
          expect((yield* server.post({}, input)).status).toBe(401);
          expect(
            (yield* server.post({ ...operate, origin: "https://example.invalid" }, input)).status,
          ).toBe(404);
          expect(store.calls).toEqual([]);
          store.shell = null;
          const gone = yield* server.post(operate, input);
          expect(gone.status).toBe(404);
          expect(JSON.parse(gone.text).code).toBe("parent_missing");
          expect(store.calls).toEqual([]);
        }),
      ),
  );

  it.effect("lists the parent's children for a read session; a gone parent is 404", () =>
    provided(
      Effect.gen(function* () {
        const store: Store = { shell: { activeRunId: null }, dispatched: [], calls: [] };
        const server = yield* serve(store);
        const listed = yield* server.get({ authorization: "Bearer reader" });
        expect(listed.status).toBe(200);
        expect(JSON.parse(listed.text).children).toEqual([
          {
            taskId: "task-live",
            title: "w",
            status: "running",
            live: true,
            environmentId: null,
            childThreadId: "thread:c",
            providerInstanceId: "claudeAgent",
            model: "claude-opus-5-5",
          },
        ]);
        expect((yield* server.get({ authorization: "Bearer reader" }, "")).status).toBe(400);
        expect((yield* server.get({})).status).toBe(401);
        store.shell = null;
        expect((yield* server.get({ authorization: "Bearer reader" })).status).toBe(404);
      }),
    ),
  );
});
