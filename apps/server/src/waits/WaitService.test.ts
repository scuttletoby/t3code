import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitForInput, WaitRecord } from "@t3tools/contracts/waitFor";
import type { Judgement } from "@t3tools/shared/waitFor";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Logger from "effect/Logger";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
import { TestClock } from "effect/testing";

import { ServerSettingsService } from "../serverSettings.ts";
import type { WaitJudges } from "./WaitJudges.ts";
import * as WaitService from "./WaitService.ts";
import { WAIT_STORE_FILE } from "./WaitStore.ts";

const NOW = Date.parse("2026-11-14T12:00:00.000Z");
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const THREAD = ThreadId.make("thread-a");
const OTHER_THREAD = ThreadId.make("thread-b");
const PARENT = ThreadId.make("thread-parent");
const PROJECT = ProjectId.make("project-a");
const OTHER_PROJECT = ProjectId.make("project-b");

const ON = { waitFor: { enabled: true } } as const;
/** A wait check with every field, as the settings hold it. */
const check = {
  argv: ["true"],
  maxArgs: 0,
  argPattern: "^[a-z]+$",
  intervalSeconds: 30,
  timeoutSeconds: 20,
  description: "",
};

/** A temp folder for the store and for the files the waits watch. */
const workspace = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-service-" });
  const stateDir = path.join(root, "state");
  yield* fs.makeDirectory(stateDir);
  return {
    root,
    stateDir,
    filePath: path.join(stateDir, WAIT_STORE_FILE),
    watched: (name: string) => path.join(root, name),
  };
});

const fileWait = (path: string, extra: Partial<WaitForInput> = {}): WaitForInput => ({
  kind: "file",
  file: { path },
  ...extra,
});

/** A record as an earlier run wrote it. */
const record = (overrides: Record<string, unknown> = {}) => ({
  id: "w_aaaaaaaaaa",
  threadId: THREAD,
  projectId: PROJECT,
  key: "0123456789abcdef",
  createdAt: "2026-11-14T11:00:00.000Z",
  deadlineAt: "2026-11-14T13:00:00.000Z",
  state: "waiting",
  attempt: 0,
  kind: "file",
  spec: { path: "/nowhere/ready", until: "exists" },
  ...overrides,
});

const storedWaits = (filePath: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return JSON.parse(yield* fs.readFileString(filePath)).waits as ReadonlyArray<WaitRecord>;
  });

const diskFull = (method: string, pathOrDescriptor: string) =>
  PlatformError.systemError({
    _tag: "Unknown",
    module: "FileSystem",
    method,
    description: "disk full",
    pathOrDescriptor,
  });

/**
 * The real file system, which records every call on a path under `watchedRoot` and fails renames
 * onto `failRenamesTo` while `failing()` says so (an atomic write ends in a rename).
 */
const instrumentedFs = (options: {
  readonly watchedRoot: string;
  readonly failRenamesTo?: string;
  readonly failing?: () => boolean;
}) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const touched: string[] = [];
    const instrumented = new Proxy(fs, {
      get(target, property, receiver) {
        const value = Reflect.get(target, property, receiver);
        if (typeof value !== "function") return value;
        return (...args: unknown[]) => {
          const first = args[0];
          if (typeof first === "string" && first.startsWith(options.watchedRoot)) {
            touched.push(`${String(property)} ${first}`);
          }
          if (
            property === "rename" &&
            args[1] === options.failRenamesTo &&
            (options.failing?.() ?? true)
          ) {
            return Effect.fail(diskFull("rename", String(args[1])));
          }
          return value.apply(target, args);
        };
      },
    });
    return { fs: instrumented, touched };
  });

/** Collects error and warning messages logged by the effect it is provided to. */
const logCollector = () => {
  const errors: string[] = [];
  const warnings: string[] = [];
  const logger = Logger.make(({ logLevel, message }) => {
    const text = String(Array.isArray(message) ? message[0] : message);
    if (logLevel === "Error") errors.push(text);
    if (logLevel === "Warn") warnings.push(text);
  });
  return { errors, warnings, layer: Logger.layer([logger], { mergeWithExisting: false }) };
};

/** A fake send that records every message and can be told to fail. */
const fakeSend = () => {
  const sent: WaitService.WaitMessage[] = [];
  let fail: WaitService.WaitSendError | null = null;
  const send: WaitService.WaitSend = (message) =>
    fail === null
      ? Effect.sync(() => {
          sent.push(message);
        })
      : Effect.fail(fail);
  return {
    sent,
    send,
    failWith: (error: WaitService.WaitSendError | null) => {
      fail = error;
    },
  };
};

const makeService = (input: {
  readonly filePath: string;
  readonly send?: WaitService.WaitSend;
  readonly judges?: WaitJudges;
  readonly fs?: FileSystem.FileSystem;
}) => {
  const made = WaitService.make({
    filePath: input.filePath,
    send: input.send ?? fakeSend().send,
    ...(input.judges === undefined ? {} : { judges: input.judges }),
  });
  return input.fs === undefined
    ? made
    : made.pipe(Effect.provideService(FileSystem.FileSystem, input.fs));
};

const register = (service: WaitService.WaitService, waitInput: WaitForInput, threadId = THREAD) =>
  service.register({ threadId, projectId: PROJECT, input: waitInput });

const setEnabled = (enabled: boolean) =>
  Effect.gen(function* () {
    const settings = yield* ServerSettingsService;
    yield* settings.updateSettings({ waitFor: { enabled } });
  });

it.layer(NodeServices.layer)("WaitService", (it) => {
  it.effect("T5: a condition already met is answered at once; nothing is stored or sent", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      const { filePath, watched } = yield* workspace;
      const ready = watched("ready");
      yield* fs.writeFileString(ready, "");
      const send = fakeSend();
      const service = yield* makeService({ filePath, send: send.send });

      const result = yield* register(service, fileWait(ready));
      assert.strictEqual(result.state, "met");
      if (result.state !== "met") return;
      assert.strictEqual(result.outcome, "exists");

      const gone = yield* register(
        service,
        fileWait(watched("missing"), { file: { path: watched("missing"), until: "gone" } }),
      );
      assert.strictEqual(gone.state, "met");
      assert.isFalse(yield* fs.exists(filePath));
      assert.deepStrictEqual(send.sent, []);
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("T7: the same condition twice is one wait; after it ended a new one is made", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      const { filePath, watched } = yield* workspace;
      const service = yield* makeService({ filePath });
      const target = watched("ready");

      const first = yield* register(service, fileWait(target, { note: "build done" }));
      const second = yield* register(service, fileWait(target, { timeoutMinutes: 5 }));
      assert.strictEqual(first.state, "waiting");
      assert.strictEqual(second.state, "waiting");
      if (first.state !== "waiting" || second.state !== "waiting") return;
      assert.match(first.waitId, /^w_[a-z2-7]{10}$/u);
      assert.isUndefined(first.deduped);
      assert.strictEqual(second.waitId, first.waitId);
      assert.isTrue(second.deduped);
      assert.include(first.next, `[wait ${first.waitId}]`);
      assert.strictEqual(first.deadlineAt, "2026-11-15T12:00:00.000Z");
      assert.strictEqual((yield* storedWaits(filePath)).length, 1);

      // Another thread waiting for the same file has its own wait.
      const other = yield* register(service, fileWait(target), OTHER_THREAD);
      assert.strictEqual(other.state, "waiting");
      if (other.state !== "waiting") return;
      assert.notStrictEqual(other.waitId, first.waitId);

      // Met but not yet delivered is still open: the same wait again.
      yield* fs.writeFileString(target, "");
      yield* TestClock.adjust(5_000);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 2, ended: 2 });
      const stored = yield* storedWaits(filePath);
      assert.deepStrictEqual(
        stored.map((wait) => [wait.state, wait.result?.outcome]),
        [
          ["delivering", "exists"],
          ["delivering", "exists"],
        ],
      );
      const third = yield* register(service, fileWait(target));
      assert.strictEqual(third.state === "waiting" && third.waitId, first.waitId);

      // A wait being sent cannot be cancelled, so the same call still finds it.
      yield* fs.remove(target);
      const late = yield* service.cancel({ projectId: PROJECT, waitId: other.waitId });
      assert.deepStrictEqual(late.cancelled, []);

      // Once a wait ended (cancelled here), the same call is a new wait.
      const later = watched("later");
      const fifth = yield* register(service, fileWait(later), OTHER_THREAD);
      if (fifth.state !== "waiting") return assert.fail("not waiting");
      yield* service.cancel({ projectId: PROJECT, waitId: fifth.waitId });
      const sixth = yield* register(service, fileWait(later), OTHER_THREAD);
      assert.isTrue(sixth.state === "waiting" && sixth.waitId !== fifth.waitId);
      assert.isTrue(sixth.state === "waiting" && sixth.deduped === undefined);
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("T6: a cancel while a slow judge runs wins; nothing is sent", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath, watched } = yield* workspace;
      const started = yield* Deferred.make<void>();
      const release = yield* Deferred.make<void>();
      const send = fakeSend();
      const judges: WaitJudges = {
        file: (_target, context) =>
          context.waitId === null
            ? Effect.succeed<Judgement>({ type: "notYet" })
            : Deferred.succeed(started, undefined).pipe(
                Effect.andThen(Deferred.await(release)),
                Effect.as<Judgement>({ type: "met", outcome: "exists", detail: "Exists." }),
              ),
      };
      const service = yield* makeService({ filePath, send: send.send, judges });
      const wait = yield* register(service, fileWait(watched("slow")));
      assert.strictEqual(wait.state, "waiting");
      if (wait.state !== "waiting") return;

      yield* TestClock.adjust(5_000);
      const sweep = yield* Effect.forkChild(service.judgeDue());
      yield* Deferred.await(started);
      // A second sweep while the first runs does not overlap it.
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 0, ended: 0 });
      const cancelled = yield* service.cancel({ projectId: PROJECT, waitId: wait.waitId });
      assert.deepStrictEqual(cancelled, { cancelled: [wait.waitId], notCancelled: [] });
      yield* Deferred.succeed(release, undefined);
      assert.deepStrictEqual(yield* Fiber.join(sweep), { judged: 1, ended: 0 });

      const [stored] = yield* storedWaits(filePath);
      assert.strictEqual(stored?.state, "cancelled");
      assert.isUndefined(stored?.result);
      assert.deepStrictEqual(send.sent, []);

      // Cancel again: reported, unchanged.
      const again = yield* service.cancel({ projectId: PROJECT, waitId: wait.waitId });
      assert.deepStrictEqual(again, {
        cancelled: [],
        notCancelled: [{ waitId: wait.waitId, state: "cancelled" }],
      });
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("T4: after a restart a file made while the server was down is met, not timed out", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      const { filePath, watched } = yield* workspace;
      const target = watched("made-while-down");
      const before = yield* makeService({ filePath });
      const wait = yield* register(before, fileWait(target, { timeoutMinutes: 10 }));
      assert.strictEqual(wait.state, "waiting");

      // The server stops; the file appears; it starts again after the deadline.
      yield* fs.writeFileString(target, "");
      yield* TestClock.adjust(2 * HOUR);
      const after = yield* makeService({ filePath });
      assert.deepStrictEqual(yield* after.judgeDue(), { judged: 1, ended: 1 });
      const [stored] = yield* storedWaits(filePath);
      assert.strictEqual(stored?.state, "delivering");
      assert.strictEqual(stored?.result?.outcome, "exists");

      // Judging again changes nothing.
      yield* TestClock.adjust(MINUTE);
      assert.deepStrictEqual(yield* after.judgeDue(), { judged: 0, ended: 0 });
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("judges each wait at its kind's interval and times it out at the deadline", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath, watched } = yield* workspace;
      const service = yield* makeService({ filePath });
      yield* register(service, fileWait(watched("never"), { timeoutMinutes: 1 }));

      // Judged at registration; the next judge is 5 s later.
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 0, ended: 0 });
      yield* TestClock.adjust(5_000);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 0, ended: 0 });
      yield* TestClock.adjust(MINUTE);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
      const [stored] = yield* storedWaits(filePath);
      assert.strictEqual(stored?.result?.outcome, "timeout");
      assert.strictEqual(stored?.result?.detail, "It does not exist yet.");
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect(
    "a judge that errors keeps the wait open, then ends it with check_error after the grace",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const { filePath, watched } = yield* workspace;
        const judges: WaitJudges = {
          file: () =>
            Effect.succeed<Judgement>({
              type: "failed",
              message: "Permission denied",
              permanent: false,
            }),
        };
        const service = yield* makeService({ filePath, judges });
        yield* register(service, fileWait(watched("locked")));
        yield* TestClock.adjust(5_000);
        yield* service.judgeDue();
        const [first] = yield* storedWaits(filePath);
        assert.strictEqual(first?.state, "waiting");
        assert.strictEqual(first?.lastError, "Permission denied");

        yield* TestClock.adjust(16 * MINUTE);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
        const [last] = yield* storedWaits(filePath);
        assert.strictEqual(last?.result?.outcome, "check_error");
      }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect(
    "refusals: invalid, too many, kinds this build cannot judge, a timeout past the maximum",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const fs = yield* FileSystem.FileSystem;
        const { filePath, watched } = yield* workspace;
        const service = yield* makeService({ filePath });
        const code = (input: WaitForInput) =>
          register(service, input).pipe(
            Effect.flip,
            Effect.map((error) => error.code),
          );

        assert.strictEqual(yield* code(fileWait("relative/path")), "invalid");
        assert.strictEqual(
          yield* code({ kind: "check", check: { name: "build" } }),
          "unknown_check",
        );
        assert.strictEqual(yield* code({ kind: "pr", pr: { number: 7 } }), "not_supported");
        assert.strictEqual(
          yield* code(fileWait(watched("x"), { timeoutMinutes: 168 * 60 + 1 })),
          "invalid",
        );
        assert.isFalse(yield* fs.exists(filePath));

        yield* register(service, fileWait(watched("one")));
        yield* register(service, fileWait(watched("two")));
        assert.strictEqual(yield* code(fileWait(watched("three"))), "too_many");
        // The same open condition is still found at the limit.
        const again = yield* register(service, fileWait(watched("one")));
        assert.isTrue(again.state === "waiting" && again.deduped === true);
      }).pipe(
        Effect.provide(
          ServerSettingsService.layerTest({ waitFor: { enabled: true, maxPerThread: 2 } }),
        ),
      ),
  );

  it.effect(
    "list shows the caller's waits or its project's, the project's checks, and the parent stored at registration",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const { filePath, watched } = yield* workspace;
        const service = yield* makeService({ filePath });
        const mine = yield* service.register({
          threadId: THREAD,
          projectId: PROJECT,
          parentThreadId: PARENT,
          input: fileWait(watched("mine")),
        });
        const theirs = yield* register(service, fileWait(watched("theirs")), OTHER_THREAD);
        if (mine.state !== "waiting" || theirs.state !== "waiting")
          return assert.fail("not waiting");
        yield* service.cancel({ projectId: PROJECT, waitId: theirs.waitId });

        const own = yield* service.list({ threadId: THREAD, projectId: PROJECT });
        assert.isTrue(own.enabled);
        assert.isUndefined(own.storeError);
        assert.deepStrictEqual(
          own.waits.map((wait) => [wait.waitId, wait.target, wait.state]),
          [[mine.waitId, `file ${watched("mine")}`, "waiting"]],
        );
        assert.deepStrictEqual(own.checks, [
          { name: "build", description: "The build is green.", maxArgs: 0 },
          { name: "deploy", description: "", maxArgs: 1 },
        ]);

        const project = yield* service.list({
          threadId: THREAD,
          projectId: PROJECT,
          scope: "project",
          includeEnded: true,
        });
        assert.deepStrictEqual(
          project.waits.map((wait) => wait.state),
          ["waiting", "cancelled"],
        );
        const elsewhere = yield* service.list({
          threadId: THREAD,
          projectId: OTHER_PROJECT,
          scope: "project",
          includeEnded: true,
        });
        assert.deepStrictEqual(elsewhere.waits, []);

        const stored = yield* storedWaits(filePath);
        assert.strictEqual(stored[0]?.parentThreadId, PARENT);
        assert.isUndefined(stored[1]?.parentThreadId);
      }).pipe(
        Effect.provide(
          ServerSettingsService.layerTest({
            waitFor: { enabled: true },
            waitChecks: {
              deploy: { ...check, argv: ["deploy-check"], maxArgs: 1 },
              build: { ...check, argv: ["build-check"], description: "The build is green." },
            },
          }),
        ),
      ),
  );

  it.effect(
    "cancel by thread also ends waits handed over from it, only in the caller's project",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const fs = yield* FileSystem.FileSystem;
        const { filePath } = yield* workspace;
        yield* fs.writeFileString(
          filePath,
          JSON.stringify({
            version: 1,
            waits: [
              record({ id: "w_aaaaaaaaaa", threadId: OTHER_THREAD }),
              // Handed over from THREAD to its parent, still being sent: open, but not cancellable.
              record({ id: "w_bbbbbbbbbb", threadId: PARENT, via: [THREAD], state: "delivering" }),
              record({ id: "w_cccccccccc", threadId: PARENT, via: [THREAD] }),
              record({ id: "w_dddddddddd", threadId: THREAD, projectId: OTHER_PROJECT }),
            ],
          }),
        );
        const service = yield* makeService({ filePath });

        const result = yield* service.cancel({ projectId: PROJECT, threadId: THREAD });
        assert.deepStrictEqual(result, {
          cancelled: ["w_cccccccccc"],
          notCancelled: [{ waitId: "w_bbbbbbbbbb", state: "delivering" }],
        });
        const notFound = yield* service
          .cancel({ projectId: PROJECT, waitId: "w_dddddddddd" })
          .pipe(Effect.flip);
        assert.strictEqual(notFound.code, "not_found");
        const both = yield* service
          .cancel({ projectId: PROJECT, waitId: "w_aaaaaaaaaa", threadId: THREAD })
          .pipe(Effect.flip);
        assert.strictEqual(both.code, "invalid");
        const neither = yield* service.cancel({ projectId: PROJECT }).pipe(Effect.flip);
        assert.strictEqual(neither.code, "invalid");
        assert.deepStrictEqual(
          (yield* storedWaits(filePath)).map((wait) => wait.state),
          ["waiting", "delivering", "cancelled", "waiting"],
        );
      }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("prune removes ended waits after keepEndedHours and writes only when one went", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      const { filePath, watched } = yield* workspace;
      const service = yield* makeService({ filePath });
      const ended = yield* register(service, fileWait(watched("a")));
      yield* register(service, fileWait(watched("b")));
      if (ended.state !== "waiting") return assert.fail("not waiting");
      yield* service.cancel({ projectId: PROJECT, waitId: ended.waitId });

      assert.strictEqual(yield* service.prune(), 0);
      const before = yield* fs.readFileString(filePath);
      yield* TestClock.adjust(24 * HOUR);
      assert.strictEqual(yield* service.prune(), 1);
      assert.deepStrictEqual(
        (yield* storedWaits(filePath)).map((wait) => wait.state),
        ["waiting"],
      );
      assert.notStrictEqual(yield* fs.readFileString(filePath), before);
      assert.strictEqual(yield* service.prune(), 0);
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect(
    "off: nothing is read or written; list reports a bad file without touching it; on again it is repaired",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const fs = yield* FileSystem.FileSystem;
        const { stateDir, filePath, watched } = yield* workspace;
        const text = `{"version":1,"waits":[{"threadId":"thread-a"`;
        yield* fs.writeFileString(filePath, text);
        const io = yield* instrumentedFs({ watchedRoot: stateDir });
        const send = fakeSend();
        const service = yield* makeService({ filePath, send: send.send, fs: io.fs });

        const refused = yield* register(service, fileWait(watched("x"))).pipe(Effect.flip);
        assert.strictEqual(refused.code, "waits_off");
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 0, ended: 0 });
        assert.strictEqual(yield* service.prune(), 0);
        assert.strictEqual(yield* service.sendNotices(), 0);
        assert.deepStrictEqual(io.touched, []);

        // List and cancel still work while off (rollback), but never repair.
        const listed = yield* service.list({ threadId: THREAD, projectId: PROJECT });
        assert.isFalse(listed.enabled);
        assert.include(listed.storeError ?? "", "repaired when waits are switched on");
        assert.strictEqual(yield* fs.readFileString(filePath), text);
        assert.deepStrictEqual(
          (yield* fs.readDirectory(stateDir)).filter((name) => name !== WAIT_STORE_FILE),
          [],
        );

        yield* setEnabled(true);
        const after = yield* service.list({ threadId: THREAD, projectId: PROJECT });
        assert.isUndefined(after.storeError);
        assert.strictEqual(yield* service.sendNotices(), 1);
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [THREAD],
        );
      }).pipe(Effect.provide(ServerSettingsService.layerTest())),
  );

  it.effect(
    "a corrupt store: the lost threads are logged and each is told once, even across a restart",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const fs = yield* FileSystem.FileSystem;
        const { stateDir, filePath, watched } = yield* workspace;
        yield* fs.writeFileString(
          filePath,
          `{"version":1,"waits":[{"id":"w_aaaaaaaaaa","threadId":"thread-a"},{"threadId":"thread-b"`,
        );
        const logs = logCollector();
        const send = fakeSend();
        const service = yield* makeService({ filePath, send: send.send });

        // Any first use loads (and repairs) the file; the service starts empty.
        const wait = yield* register(service, fileWait(watched("x")), PARENT).pipe(
          Effect.provide(logs.layer),
        );
        assert.strictEqual(wait.state, "waiting");
        assert.strictEqual(logs.errors.length, 1);
        assert.include(logs.errors[0]!, "thread-a");
        assert.include(logs.errors[0]!, "thread-b");
        assert.include(logs.errors[0]!, ".corrupt-");
        assert.strictEqual(
          (yield* fs.readDirectory(stateDir)).filter((name) => name.includes(".corrupt-")).length,
          1,
        );

        // The first send fails for a while; the notices stay in the file.
        send.failWith(new WaitService.WaitSendError({ detail: "busy", retry: true }));
        assert.strictEqual(yield* service.sendNotices(), 0);
        send.failWith(null);

        // A restart before they were sent: the new service sends them, with the same ids.
        const restarted = yield* makeService({ filePath, send: send.send });
        assert.strictEqual(yield* restarted.sendNotices(), 2);
        assert.deepStrictEqual(
          send.sent.map((message) => [message.threadId, message.commandId]),
          [
            ["thread-a", `${send.sent[0]!.messageId}:0`],
            ["thread-b", `${send.sent[1]!.messageId}:0`],
          ],
        );
        assert.include(send.sent[0]!.text, "[wait store]");
        assert.strictEqual(yield* restarted.sendNotices(), 0);
        const stored = JSON.parse(yield* fs.readFileString(filePath));
        assert.isUndefined(stored.notices);
        assert.strictEqual(stored.waits.length, 1);
      }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("a notice that can never be sent is dropped with an error", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      const { filePath } = yield* workspace;
      yield* fs.writeFileString(filePath, `{"threadId":"thread-a"`);
      const logs = logCollector();
      const send = fakeSend();
      send.failWith(new WaitService.WaitSendError({ detail: "thread deleted", retry: false }));
      const service = yield* makeService({ filePath, send: send.send });

      assert.strictEqual(yield* service.sendNotices().pipe(Effect.provide(logs.layer)), 0);
      assert.isTrue(logs.errors.some((line) => line.includes("thread deleted")));
      send.failWith(null);
      assert.strictEqual(yield* service.sendNotices(), 0);
      assert.deepStrictEqual(send.sent, []);
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect(
    "a newer store is never read or written: register and cancel refuse, list shows why",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const fs = yield* FileSystem.FileSystem;
        const { stateDir, filePath, watched } = yield* workspace;
        const text = JSON.stringify({ version: 2, waits: [{ id: "w_aaaaaaaaaa", shape: "new" }] });
        yield* fs.writeFileString(filePath, text);
        const service = yield* makeService({ filePath });

        const refused = yield* register(service, fileWait(watched("x"))).pipe(Effect.flip);
        assert.strictEqual(refused.code, "store_unavailable");
        assert.include(refused.message, "newer version");
        const cancel = yield* service
          .cancel({ projectId: PROJECT, threadId: THREAD })
          .pipe(Effect.flip);
        assert.strictEqual(cancel.code, "store_unavailable");
        const listed = yield* service.list({ threadId: THREAD, projectId: PROJECT });
        assert.include(listed.storeError ?? "", "newer version");
        assert.deepStrictEqual(listed.waits, []);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 0, ended: 0 });
        assert.strictEqual(yield* service.prune(), 0);
        assert.strictEqual(yield* fs.readFileString(filePath), text);
        assert.deepStrictEqual(yield* fs.readDirectory(stateDir), [WAIT_STORE_FILE]);

        // Once the file is one this build reads (the newer build moved it away), waits work again.
        yield* fs.remove(filePath);
        const wait = yield* register(service, fileWait(watched("x")));
        assert.strictEqual(wait.state, "waiting");
      }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect(
    "a failed write registers nothing, and a failed write after a judge leaves the wait to be judged again",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const fs = yield* FileSystem.FileSystem;
        const { stateDir, filePath, watched } = yield* workspace;
        let failing = true;
        const io = yield* instrumentedFs({
          watchedRoot: stateDir,
          failRenamesTo: filePath,
          failing: () => failing,
        });
        const service = yield* makeService({ filePath, fs: io.fs });
        const target = watched("ready");

        const refused = yield* register(service, fileWait(target)).pipe(Effect.flip);
        assert.strictEqual(refused.code, "store_unavailable");
        assert.deepStrictEqual(
          (yield* service.list({ threadId: THREAD, projectId: PROJECT })).waits,
          [],
        );
        assert.isFalse(yield* fs.exists(filePath));

        failing = false;
        const wait = yield* register(service, fileWait(target));
        if (wait.state !== "waiting") return assert.fail("not waiting");

        failing = true;
        yield* fs.writeFileString(target, "");
        yield* TestClock.adjust(5_000);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
        assert.strictEqual((yield* storedWaits(filePath))[0]?.state, "waiting");
        const listed = yield* service.list({ threadId: THREAD, projectId: PROJECT });
        assert.strictEqual(listed.waits[0]?.state, "waiting");

        failing = false;
        yield* TestClock.adjust(5_000);
        assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
        assert.strictEqual((yield* storedWaits(filePath))[0]?.state, "delivering");
      }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );
});
