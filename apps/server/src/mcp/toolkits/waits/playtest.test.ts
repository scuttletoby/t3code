/**
 * Fork (playtest): the `playtest` tool as the server registers it, over the real playtest service
 * and its fake adapter (PlaytestService.testkit.ts). The service's own rules are tested there; these
 * tests show the tool reaches them for the calling thread only and says the answer plainly.
 */
import { assert, describe, expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  type ProviderInteractionMode,
  ProviderInstanceId,
  type RuntimeMode,
} from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import { McpSchema, McpServer } from "effect/ai";

import * as ThreadManagement from "../../../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../../../peer/PeerForwarding.ts";
import * as RemoteDelegation from "../../../peer/RemoteDelegation.ts";
import { Playtest } from "../../../playtest/PlaytestLayer.ts";
import type * as PlaytestService from "../../../playtest/PlaytestService.ts";
import {
  adapter,
  fixture,
  holdStep,
  makeService,
  openGate,
  OTHER_THREAD,
  ran,
  removeFile,
  setFile,
  settingsWith,
  stepStarted,
  storedSession,
  THREAD,
  live,
  writeStore,
} from "../../../playtest/PlaytestService.testkit.ts";
import { LaneStore } from "../../../fork/lanes/LaneStore.ts";
import { ServerSettingsService } from "../../../serverSettings.ts";
import { Waits } from "../../../waits/WaitWorker.ts";
import * as McpHttpServer from "../../McpHttpServer.ts";
import * as McpInvocationContext from "../../McpInvocationContext.ts";
import * as McpToolAccessTestkit from "../../McpToolAccess.testkit.ts";

const scopeOf = (
  overrides: Partial<McpInvocationContext.McpInvocationScope> = {},
): McpInvocationContext.McpInvocationScope => ({
  environmentId: EnvironmentId.make("playtest-environment"),
  requestNamespace: "playtest-session",
  thread: {
    threadId: THREAD,
    providerSessionId: "playtest-session",
    providerInstanceId: ProviderInstanceId.make("codex"),
  },
  client: undefined,
  issuedAt: 0,
  capabilities: new Set(["orchestration"]),
  ...overrides,
});

const client = McpSchema.McpServerClient.of({
  clientId: 1,
  protocolVersion: "2025-06-18",
  clientCapabilities: {},
  clientInfo: { name: "playtest", version: "1" },
  initializePayload: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "playtest", version: "1" },
  },
  getClient: Effect.die("unused"),
});

/** The calling thread's modes as the server records them; a test may change them between calls. */
type Modes = {
  runtimeMode: RuntimeMode;
  interactionMode: ProviderInteractionMode;
};

/** The fork's tools as the server registers them, with this playtest service behind them. */
const toolsLayer = (
  service: PlaytestService.PlaytestService,
  modes: Modes = { runtimeMode: "full-access", interactionMode: "default" },
) =>
  McpHttpServer.layerForkTools.pipe(
    Layer.provideMerge(McpHttpServer.layerInProcessMcpServer),
    Layer.provideMerge(
      Layer.mergeAll(
        ServerSettingsService.layerTest({}),
        Layer.mock(ThreadManagement.ThreadManagementService)({
          getThreadShell: (threadId) =>
            Effect.sync(() => McpToolAccessTestkit.liveThreadShell(threadId, { ...modes })),
        }),
        // The other fork tools' services; these tests never call them.
        Layer.mock(Waits)({}),
        Layer.mock(LaneStore)({}),
        Layer.mock(RemoteDelegation.RemoteDelegation)({}),
        Layer.mock(PeerForwarding.PeerForwarding)({}),
        Layer.succeed(Playtest, service),
      ),
    ),
  );

const call = (
  args: Record<string, unknown>,
  scope: McpInvocationContext.McpInvocationScope = scopeOf(),
) =>
  Effect.gen(function* () {
    const server = yield* McpServer.McpServer;
    return yield* server
      .callTool({ name: "playtest", arguments: args })
      .pipe(
        Effect.provideService(McpInvocationContext.McpInvocationContext, scope),
        Effect.provideService(McpSchema.McpServerClient, client),
      );
  });

type Answer = {
  readonly action: string;
  readonly outcome: string;
  readonly message: string;
  readonly session: (Record<string, unknown> & { readonly id: string }) | null;
};

const succeeded = (result: McpSchema.CallToolResult) => {
  assert.notStrictEqual(result.isError, true, JSON.stringify(result.content));
  return result.structuredContent as Answer;
};

const failed = (result: McpSchema.CallToolResult) => {
  const text = result.content[0];
  assert.isTrue(result.isError === true && text?.type === "text", "expected a declared failure");
  return JSON.parse(text?.type === "text" ? text.text : "{}") as {
    readonly code: string;
    readonly message: string;
  };
};

/** Refused before the handler runs: the MCP layer's argument check or the input schema. */
const refused = (args: Record<string, unknown>) =>
  call(args).pipe(
    Effect.map((result) => {
      // A schema refusal may come back as an error result rather than a protocol error.
      assert.isTrue(result.isError === true, JSON.stringify(result.content));
      const text = result.content[0];
      return text?.type === "text" ? text.text : "";
    }),
    Effect.catch((error) => Effect.succeed(String((error as { message?: unknown }).message))),
  );

const stepsRan = (dir: string) =>
  ran(dir).pipe(Effect.map((lines) => lines.filter((line) => !line.startsWith("finished"))));

describe.skipIf(HostProcess.Platform.defaultValue() === "win32")("playtest tool", () => {
  it.live("start, push, status and end act on the calling thread's session", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        yield* setFile(dir, "deploy.out", "deployed to dev-1");
        yield* Effect.gen(function* () {
          const started = succeeded(
            yield* call({ action: "start", target: "dev-1", note: "shop" }),
          );
          expect(started).toMatchObject({ action: "start", outcome: "started" });
          expect(started.session).toMatchObject({
            threadId: THREAD,
            target: "dev-1",
            note: "shop",
          });
          assert.notProperty(started.session, "adapter");
          assert.include(started.message, "call push");
          const id = started.session?.id;

          const pushed = succeeded(yield* call({ action: "push" }));
          expect(pushed).toMatchObject({ action: "push", outcome: "done" });
          expect(pushed.session).toMatchObject({ id, rounds: 1 });
          assert.include(pushed.message, "Round 1 done (step deploy): deployed to dev-1");

          const status = succeeded(yield* call({ action: "status" }));
          expect(status).toMatchObject({ action: "status", outcome: "on" });
          expect(status.session).toMatchObject({ id, state: "active", rounds: 1 });

          const ended = succeeded(yield* call({ action: "end" }));
          expect(ended).toMatchObject({ action: "end", outcome: "ended" });
          expect(ended.session).toMatchObject({ id, state: "ended", endReason: "agent" });

          // Runs again: nothing to end, and status shows the last session as ended.
          const again = succeeded(yield* call({ action: "end" }));
          expect(again).toMatchObject({ outcome: "nothing-to-end" });
          assert.include(again.message, "nothing to end");
          expect(succeeded(yield* call({ action: "status" }))).toMatchObject({
            outcome: "ended",
            session: { id },
          });
          expect(failed(yield* call({ action: "push" }))).toMatchObject({ code: "no_session" });
        }).pipe(Effect.provide(toolsLayer(service)));
        // The thread the steps were given is the caller's own.
        assert.deepStrictEqual(yield* stepsRan(dir), [
          "claim:0",
          "build:1",
          "deploy:1",
          "release:1",
        ]);
      }),
    ),
  );

  it.live("with playtest off or absent start and push are refused and nothing is written", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const fs = yield* FileSystem.FileSystem;
        // Off with every step set, and never set at all.
        for (const playtest of [adapter(fx, dir, { enabled: false }), undefined]) {
          const service = yield* makeService(fx, yield* settingsWith(playtest));
          yield* Effect.gen(function* () {
            const start = failed(yield* call({ action: "start", target: "dev-1" }));
            expect(start).toMatchObject({ code: "playtest_off" });
            assert.include(start.message, "Only a person can switch it on in the settings");
            expect(failed(yield* call({ action: "push" }))).toMatchObject({
              code: expect.stringMatching(/^(playtest_off|no_session)$/u),
            });
            // Nothing to show or end, and saying so writes nothing either.
            expect(succeeded(yield* call({ action: "status" }))).toMatchObject({
              outcome: "none",
              session: null,
            });
            expect(succeeded(yield* call({ action: "end" }))).toMatchObject({
              outcome: "nothing-to-end",
              session: null,
            });
          }).pipe(Effect.provide(toolsLayer(service)));
        }
        assert.deepStrictEqual(yield* ran(dir), []);
        assert.isFalse(yield* fs.exists(fx.filePath));
        assert.isFalse(yield* fs.exists(fx.stepsFolder));
      }),
    ),
  );

  it.live("with no session status and end say so plainly and change nothing", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        yield* Effect.gen(function* () {
          const status = succeeded(yield* call({ action: "status" }));
          expect(status).toMatchObject({ outcome: "none", session: null });
          assert.include(status.message, "This thread has no playtest session");
          assert.include(status.message, "Start one with action start");
          for (let i = 0; i < 2; i += 1) {
            const end = succeeded(yield* call({ action: "end" }));
            expect(end).toMatchObject({ outcome: "nothing-to-end", session: null });
            assert.include(end.message, "nothing to end");
          }
          const push = failed(yield* call({ action: "push" }));
          expect(push).toMatchObject({ code: "no_session" });
        }).pipe(Effect.provide(toolsLayer(service)));
        assert.deepStrictEqual(yield* ran(dir), []);
        assert.isFalse(yield* (yield* FileSystem.FileSystem).exists(fx.filePath));
      }),
    ),
  );

  it.live("start twice is the same session; a second claim never runs", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        yield* Effect.gen(function* () {
          const first = succeeded(yield* call({ action: "start", target: "dev-1" }));
          const second = succeeded(yield* call({ action: "start", target: "dev-1" }));
          expect(second).toMatchObject({
            outcome: "already-on",
            session: { id: first.session?.id },
          });
          assert.include(second.message, "nothing new was claimed");
          // Another target while one is on: refused, with what to do.
          const other = failed(yield* call({ action: "start", target: "dev-2" }));
          expect(other).toMatchObject({ code: "busy" });
          assert.include(other.message, "already playtests dev-1");
        }).pipe(Effect.provide(toolsLayer(service)));
        assert.deepStrictEqual(yield* stepsRan(dir), ["claim:0"]);
      }),
    ),
  );

  it.live("a push while a round runs is refused as busy; status shows the running round", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        yield* Effect.gen(function* () {
          succeeded(yield* call({ action: "start", target: "dev-1" }));
          yield* holdStep(dir, "build");
          const round = yield* Effect.forkChild(call({ action: "push" }));
          yield* stepStarted(dir, "build");

          const second = failed(yield* call({ action: "push" }));
          expect(second).toMatchObject({ code: "busy" });
          assert.include(second.message, "Round 1 runs (step build); wait for it");
          const status = succeeded(yield* call({ action: "status" }));
          expect(status).toMatchObject({ outcome: "on", session: { running: "round" } });
          assert.include(status.message, "A round step runs now");

          yield* openGate(dir, "build");
          expect(succeeded(yield* Fiber.join(round))).toMatchObject({ outcome: "done" });
        }).pipe(Effect.provide(toolsLayer(service)));
        assert.deepStrictEqual(yield* stepsRan(dir), ["claim:0", "build:1", "deploy:1"]);
      }),
    ),
  );

  it.live("another thread's session is out of reach: no thread argument, and none is used", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        yield* writeStore(fx, [storedSession(fx, dir, { threadId: OTHER_THREAD })]);
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        const fs = yield* FileSystem.FileSystem;
        const before = yield* fs.readFileString(fx.filePath);
        yield* Effect.gen(function* () {
          // The thread is the credential's; the tool does not take one.
          for (const extra of [{ threadId: OTHER_THREAD }, { thread: OTHER_THREAD }]) {
            for (const args of [
              { action: "start", target: "dev-1" },
              { action: "status" },
              { action: "push" },
              { action: "end" },
            ]) {
              assert.include(
                yield* refused({ ...args, ...extra }),
                "Unknown argument",
                args.action,
              );
            }
          }
          // The caller sees only its own (none); the other thread's session is untouched.
          expect(succeeded(yield* call({ action: "status" }))).toMatchObject({ outcome: "none" });
          expect(succeeded(yield* call({ action: "end" }))).toMatchObject({
            outcome: "nothing-to-end",
          });
          expect(failed(yield* call({ action: "push" }))).toMatchObject({ code: "no_session" });

          // Outside a thread the tool is not offered at all.
          const noThread = scopeOf({
            thread: undefined,
            client: { sessionId: "client-2", label: "Outside", access: "full-access" },
          });
          const outside = yield* call({ action: "status" }, noThread).pipe(Effect.flip);
          assert.include(String(outside.message), "not found");
          // A read-only client cannot start or end one.
          const readOnly = scopeOf({
            client: { sessionId: "client-1", label: "Viewer", access: "read-only" },
          });
          expect(failed(yield* call({ action: "end" }, readOnly))).toMatchObject({
            code: "capability_denied",
          });
        }).pipe(Effect.provide(toolsLayer(service)));
        assert.strictEqual(yield* fs.readFileString(fx.filePath), before);
        assert.deepStrictEqual(yield* ran(dir), []);
      }),
    ),
  );

  it.live("after a T3 restart mid-round status reads the stored session; push goes on", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const settings = yield* settingsWith(adapter(fx, dir));
        let id = "";
        // T3 stops while a round runs: the step is stopped and the round stays `running` on disk.
        yield* Effect.scoped(
          Effect.gen(function* () {
            const service = yield* makeService(fx, settings);
            yield* Effect.gen(function* () {
              id = succeeded(yield* call({ action: "start", target: "dev-1" })).session?.id ?? "";
              yield* holdStep(dir, "deploy");
              yield* Effect.forkScoped(call({ action: "push" }));
              yield* stepStarted(dir, "deploy");
            }).pipe(Effect.provide(toolsLayer(service)));
          }),
        );
        yield* removeFile(dir, "deploy.gate");
        yield* removeFile(dir, "deploy.started");

        // A new service reads the store file again.
        const restarted = yield* makeService(fx, settings);
        yield* Effect.gen(function* () {
          const status = succeeded(yield* call({ action: "status" }));
          expect(status).toMatchObject({
            outcome: "on",
            session: {
              id,
              state: "active",
              target: "dev-1",
              lastRound: { number: 1, state: "stopped", step: "deploy" },
            },
          });
          assert.notProperty(status.session, "running");
          assert.include(status.message, "Round 1 stopped (step deploy)");
          expect(succeeded(yield* call({ action: "push" }))).toMatchObject({
            outcome: "done",
            session: { id, rounds: 2 },
          });
          expect(succeeded(yield* call({ action: "end" }))).toMatchObject({ outcome: "ended" });
        }).pipe(Effect.provide(toolsLayer(restarted)));
      }),
    ),
  );

  it.live("an unknown action, a misplaced or extra field is refused before the service", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        yield* Effect.gen(function* () {
          for (const args of [
            { action: "restart" },
            {},
            { action: "start" },
            { action: "push", target: "dev-1" },
            { action: "end", note: "done" },
            { action: "status", target: "dev-1", note: "x" },
          ]) {
            assert.include(yield* refused(args), "Invalid parameters", JSON.stringify(args));
          }
          assert.include(yield* refused({ action: "start" }), "start needs a target.");
          assert.include(
            yield* refused({ action: "push", target: "dev-1" }),
            "push takes no target or note",
          );
          for (const extra of [
            { playtest: { enabled: true } },
            { enabled: true },
            { argv: ["x"] },
          ]) {
            assert.include(yield* refused({ action: "status", ...extra }), "Unknown argument");
          }
        }).pipe(Effect.provide(toolsLayer(service)));
        assert.deepStrictEqual(yield* ran(dir), []);
        assert.isFalse(yield* (yield* FileSystem.FileSystem).exists(fx.filePath));
      }),
    ),
  );
  // The thread's modes as the server records them: only full-access, not in plan mode, may start or
  // push. Each row starts a session as full-access, then the thread switches to the row's modes.
  it.live.each([
    { runtimeMode: "full-access", interactionMode: "default", allowed: true },
    { runtimeMode: "approval-required", interactionMode: "default", allowed: false },
    { runtimeMode: "auto-accept-edits", interactionMode: "default", allowed: false },
    { runtimeMode: "full-access", interactionMode: "plan", allowed: false },
  ] as const)(
    "$runtimeMode/$interactionMode: start and push allowed: $allowed; status and end work",
    (row) =>
      live(
        Effect.gen(function* () {
          const fx = yield* fixture;
          const dir = yield* fx.adapterDir("a");
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          const modes: Modes = { runtimeMode: "full-access", interactionMode: "default" };
          yield* Effect.gen(function* () {
            const id = succeeded(yield* call({ action: "start", target: "dev-1" })).session?.id;
            modes.runtimeMode = row.runtimeMode;
            modes.interactionMode = row.interactionMode;

            const start = yield* call({ action: "start", target: "dev-1" });
            const push = yield* call({ action: "push" });
            if (row.allowed) {
              expect(succeeded(start)).toMatchObject({ outcome: "already-on" });
              expect(succeeded(push)).toMatchObject({ outcome: "done" });
            } else {
              for (const result of [start, push]) {
                expect(failed(result)).toMatchObject({
                  code: "capability_denied",
                  message: expect.stringContaining(
                    "Only a full-access thread, not in plan mode, can start or push a playtest; ask the owner",
                  ),
                });
              }
            }
            expect(succeeded(yield* call({ action: "status" }))).toMatchObject({
              outcome: "on",
              session: { id, rounds: row.allowed ? 1 : 0 },
            });
            expect(succeeded(yield* call({ action: "end" }))).toMatchObject({
              outcome: "ended",
              session: { id, state: "ended" },
            });
            // With no session on, a start is refused the same way and claims nothing.
            const again = yield* call({ action: "start", target: "dev-1" });
            if (row.allowed) expect(succeeded(again)).toMatchObject({ outcome: "started" });
            else expect(failed(again)).toMatchObject({ code: "capability_denied" });
          }).pipe(Effect.provide(toolsLayer(service, modes)));
          const steps = yield* stepsRan(dir);
          assert.deepStrictEqual(
            steps.filter((step) => !step.startsWith("release")),
            row.allowed ? ["claim:0", "build:1", "deploy:1", "claim:0"] : ["claim:0"],
          );
        }),
      ),
  );

  it.live("a thread switched between start and push is checked again on each call", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
        const modes: Modes = { runtimeMode: "full-access", interactionMode: "default" };
        yield* Effect.gen(function* () {
          succeeded(yield* call({ action: "start", target: "dev-1" }));
          modes.runtimeMode = "approval-required";
          expect(failed(yield* call({ action: "push" }))).toMatchObject({
            code: "capability_denied",
          });
          // Switched back: the next push runs.
          modes.runtimeMode = "full-access";
          expect(succeeded(yield* call({ action: "push" }))).toMatchObject({
            outcome: "done",
            session: { rounds: 1 },
          });
          modes.interactionMode = "plan";
          expect(failed(yield* call({ action: "push" }))).toMatchObject({
            code: "capability_denied",
          });
          // A restricted thread can still release its target.
          expect(succeeded(yield* call({ action: "end" }))).toMatchObject({ outcome: "ended" });
        }).pipe(Effect.provide(toolsLayer(service, modes)));
        assert.deepStrictEqual(yield* stepsRan(dir), [
          "claim:0",
          "build:1",
          "deploy:1",
          "release:1",
        ]);
      }),
    ),
  );

  it.live("status says to call end for a claim cut off by a stop or a release not yet done", () =>
    live(
      Effect.gen(function* () {
        const fx = yield* fixture;
        const dir = yield* fx.adapterDir("a");
        for (const [state, text] of [
          ["claiming", "did not finish (T3 stopped); call end to release it."],
          ["ending", "its release has not worked yet; T3 tries again, or call end again."],
        ] as const) {
          yield* writeStore(fx, [
            storedSession(fx, dir, state === "ending" ? { state, endReason: "agent" } : { state }),
          ]);
          const service = yield* makeService(fx, yield* settingsWith(adapter(fx, dir)));
          yield* Effect.gen(function* () {
            const status = succeeded(yield* call({ action: "status" }));
            expect(status).toMatchObject({ outcome: "on", session: { state } });
            assert.notProperty(status.session, "running");
            assert.include(status.message, text, state);
          }).pipe(Effect.provide(toolsLayer(service)));
        }
        // Status runs no step.
        assert.deepStrictEqual(yield* ran(dir), []);
      }),
    ),
  );
});
