/**
 * Fork (issue 625): the startup orphan scan. State question: a rerun and a restart (one message),
 * an environment that does not answer (scanned again), a busy, stopped or gone parent, a thread
 * made after the start (its record may still come), waits off, and no links.
 */
import { describe, expect, it } from "@effect/vitest";
import {
  type CommandId,
  EnvironmentId,
  MessageId,
  type OrchestrationV2ThreadShell,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import { TestClock } from "effect/testing";

import * as ServerEnvironment from "../../environment/ServerEnvironment.ts";
import * as McpToolAccessTestkit from "../../mcp/McpToolAccess.testkit.ts";
import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../../peer/PeerForwarding.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import { at, NOW, PARENT } from "./delegatedResults.testkit.ts";
import * as DelegatedOrphanScan from "./DelegatedOrphanScan.ts";
import type { RemoteChildKey } from "./DelegatedResultsStore.ts";
import * as DelegatedResultsStore from "./DelegatedResultsStore.ts";

const HERE = EnvironmentId.make("env-here");
const ENV_B = EnvironmentId.make("env-b");
const ENV_C = EnvironmentId.make("env-c");

const OPTIONS: DelegatedOrphanScan.OrphanScanOptions = {
  delay: Duration.minutes(3),
  retryDelay: Duration.minutes(10),
  attempts: 3,
  maxPages: 3,
  pageTimeout: Duration.seconds(10),
};

const idleShell = (
  id: ThreadId,
  overrides: Partial<OrchestrationV2ThreadShell> = {},
): OrchestrationV2ThreadShell => ({
  ...McpToolAccessTestkit.liveThreadShell(id, { activeRunId: null }),
  status: "idle",
  ...overrides,
});

interface ThreadThere {
  readonly threadId: string;
  readonly from?: { readonly environmentId: EnvironmentId; readonly threadId: ThreadId };
  /** Before the start by default. */
  readonly createdAt?: number;
}

const iso = (ms: number) => DateTime.formatIso(at(ms));

/**
 * Both sides as the servers keep them, in memory. `sent` and `receipts` are shared across
 * services, as the database is across a restart.
 */
const makeWorld = (waitFor: { readonly enabled: boolean } = { enabled: true }) => {
  const shells = new Map<ThreadId, OrchestrationV2ThreadShell>([[PARENT, idleShell(PARENT)]]);
  const followed = new Map<ThreadId, ReadonlyArray<RemoteChildKey> | null>();
  const there = new Map<EnvironmentId, Array<ThreadThere>>([
    [ENV_B, [{ threadId: "lost-1" }, { threadId: "known-1" }]],
  ]);
  followed.set(PARENT, [{ environmentId: ENV_B, threadId: ThreadId.make("known-1") }]);
  const silent = new Set<EnvironmentId>();
  const linksOff = { value: false };
  const pageReads: Array<{ environmentId: EnvironmentId; cursor: number }> = [];
  const sent: Array<{ threadId: ThreadId; messageId: string; text: string }> = [];
  const receipts = new Map<string, ThreadId>();
  const page = (environmentId: EnvironmentId, cursor: number) => {
    const all = there.get(environmentId) ?? [];
    // Two threads a page, newest first, as `t3_thread_list` orders them.
    const threads = all.slice(cursor, cursor + 2).map((thread, index) => ({
      threadId: ThreadId.make(thread.threadId),
      title: `title ${thread.threadId}`,
      delegatedFrom: { ...(thread.from ?? { environmentId: HERE, threadId: PARENT }), title: "p" },
      createdAt: iso(thread.createdAt ?? NOW - 60 * 60_000),
      updatedAt: iso(NOW - (cursor + index) * 60_000),
    }));
    return {
      projectId: null,
      currentThreadId: null,
      threads,
      nextCursor: cursor + 2 < all.length ? cursor + 2 : null,
      total: all.length,
    };
  };
  return {
    shells,
    followed,
    there,
    silent,
    linksOff,
    pageReads,
    sent,
    layer: Layer.mergeAll(
      ServerSettingsService.layerTest({ waitFor }),
      Layer.mock(ServerEnvironment.ServerEnvironment)({
        getEnvironmentId: Effect.succeed(HERE),
      }),
      Layer.mock(ThreadManagement.ThreadManagementService)({
        getThreadShell: (threadId) => Effect.succeed(shells.get(threadId) ?? null),
        getMessagesById: (threadId, ids) =>
          Effect.succeed(
            new Map(
              sent
                .filter((message) => message.threadId === threadId)
                .filter((message) => ids.includes(MessageId.make(message.messageId)))
                .map((message) => [MessageId.make(message.messageId), {} as never]),
            ),
          ),
        sendToThread: (input) =>
          Effect.sync(() => {
            sent.push({ threadId: input.threadId, messageId: input.messageId!, text: input.text });
            receipts.set(input.commandId, input.threadId);
            return {} as never;
          }),
      }),
      Layer.mock(CommandReceiptStore.CommandReceiptStoreV2)({
        getByCommandId: (commandId: CommandId) =>
          Effect.succeed(
            receipts.has(commandId)
              ? Option.some({ status: "accepted", threadId: receipts.get(commandId)! } as never)
              : Option.none(),
          ),
      }),
      Layer.mock(DelegatedResultsStore.DelegatedResultsStore)({
        remoteChildrenOf: (parent) =>
          Effect.succeed(followed.has(parent) ? followed.get(parent)! : []),
      }),
      Layer.mock(PeerForwarding.PeerForwarding)({
        links: () =>
          linksOff.value
            ? Effect.fail(PeerForwarding.linkedEnvironmentsOff)
            : Effect.succeed({
                environmentId: HERE,
                links: [
                  ...[...there.keys()].map((environmentId) => ({
                    environmentId,
                    label: environmentId,
                    status: "reachable" as const,
                    access: {} as never,
                    expiresAt: "2027-01-01T00:00:00.000Z",
                    lastError: null,
                  })),
                  // An expired link is never asked.
                  {
                    environmentId: EnvironmentId.make("env-expired"),
                    label: "expired",
                    status: "expired" as const,
                    access: {} as never,
                    expiresAt: "2026-01-01T00:00:00.000Z",
                    lastError: null,
                  },
                ],
                notListed: "",
              }),
        call: ((
          _scope: unknown,
          _tool: unknown,
          environmentId: EnvironmentId,
          params: { readonly cursor: number },
        ) =>
          Effect.suspend(() => {
            pageReads.push({ environmentId, cursor: params.cursor });
            return silent.has(environmentId)
              ? Effect.never
              : Effect.succeed(page(environmentId, params.cursor));
          })) as never,
      }),
    ),
  };
};

const service = (world: ReturnType<typeof makeWorld>) =>
  DelegatedOrphanScan.make(OPTIONS).pipe(Effect.provide(world.layer));

const setClock = TestClock.setTime(NOW);

/** Lets forked fibers reach their next sleep (or finish) before the clock moves. */
const settle = Effect.repeat(Effect.yieldNow, { times: 30 });

/** A scan, while the clock moves past each page timeout of an environment that does not answer. */
const scanned = (scan: Effect.Effect<DelegatedOrphanScan.ScanOutcome>) =>
  Effect.gen(function* () {
    const fiber = yield* Effect.forkChild(scan);
    for (let step = 0; step < 3; step++) {
      yield* settle;
      yield* TestClock.adjust(OPTIONS.pageTimeout);
    }
    return yield* Fiber.join(fiber);
  });

describe("DelegatedOrphanScan.scan", () => {
  it.effect("reminds a parent once of a thread there that no task follows", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const scan = yield* service(world);
      const outcome = yield* scanned(scan.scan());
      expect(outcome.parents.get(PARENT)).toEqual({ type: "sent", threadIds: ["lost-1"] });
      expect(world.sent).toHaveLength(1);
      expect(world.sent[0]!.text).toContain("lost-1");
      expect(world.sent[0]!.text).toContain(`in ${ENV_B}`);
      expect(world.sent[0]!.text).not.toContain("known-1");
      // Runs again: the same orphans, the same message, nothing more.
      yield* scanned(scan.scan());
      expect(world.sent).toHaveLength(1);
      // The next start: a fresh service finds the message it sent.
      yield* scanned((yield* service(world)).scan());
      expect(world.sent).toHaveLength(1);
      // An expired link is never asked.
      expect(world.pageReads.map((read) => read.environmentId)).not.toContain("env-expired");
    }),
  );

  it.effect("reads every page, and one reminder names the orphans of all environments", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.there.set(ENV_B, [
        { threadId: "known-1" },
        { threadId: "lost-1" },
        { threadId: "lost-2" },
        { threadId: "lost-3" },
      ]);
      world.there.set(ENV_C, [{ threadId: "lost-c" }]);
      const outcome = yield* scanned((yield* service(world)).scan());
      expect(outcome.partial).toEqual([]);
      expect(outcome.parents.get(PARENT)).toMatchObject({ type: "sent" });
      expect(world.sent).toHaveLength(1);
      for (const id of ["lost-1", "lost-2", "lost-3", "lost-c"]) {
        expect(world.sent[0]!.text).toContain(id);
      }
    }),
  );

  it.effect("names an environment with more threads than the page limit as partial", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.there.set(
        ENV_B,
        Array.from({ length: 10 }, (_, index) => ({ threadId: `lost-${index}` })),
      );
      const outcome = yield* scanned((yield* service(world)).scan());
      expect(outcome.partial).toEqual([ENV_B]);
      expect(world.pageReads).toHaveLength(OPTIONS.maxPages);
      // What it read is still reminded.
      expect(world.sent).toHaveLength(1);
    }),
  );

  it.effect("stops paging at threads last updated before the window", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      // Ten pages, but the clock is a day and more past the second one.
      world.there.set(
        ENV_B,
        Array.from({ length: 20 }, (_, index) => ({ threadId: `t-${index}` })),
      );
      yield* TestClock.setTime(NOW + 24 * 60 * 60_000 + 60_000);
      const outcome = yield* scanned((yield* service(world)).scan());
      expect(outcome.partial).toEqual([]);
      expect(world.pageReads).toHaveLength(1);
    }),
  );

  it.effect("never names a thread made after the start, or one from another environment", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.there.set(ENV_B, [
        // Launched after this start: its record here may still be written.
        { threadId: "new-1", createdAt: NOW + 1 },
        { threadId: "new-2", createdAt: NOW },
        // Launched by a thread of the same id in another environment.
        { threadId: "foreign", from: { environmentId: ENV_C, threadId: PARENT } },
        { threadId: "known-1" },
      ]);
      const scan = yield* service(world);
      yield* TestClock.adjust(Duration.minutes(5));
      const outcome = yield* scanned(scan.scan());
      expect(outcome.parents.size).toBe(1);
      expect(outcome.parents.get(PARENT)).toEqual({ type: "none" });
      expect(world.sent).toHaveLength(0);
    }),
  );

  it.effect("never writes to a busy, stopped, archived or deleted parent", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const scan = yield* service(world);
      world.shells.set(
        PARENT,
        idleShell(PARENT, { status: "running", activityRunStatus: "running" }),
      );
      expect((yield* scanned(scan.scan())).parents.get(PARENT)).toEqual({ type: "busy" });
      world.shells.set(PARENT, idleShell(PARENT, { status: "interrupted" }));
      expect((yield* scanned(scan.scan())).parents.get(PARENT)).toEqual({
        type: "held",
        detail: "stopped",
      });
      world.shells.set(PARENT, idleShell(PARENT, { archivedAt: at(NOW) }));
      expect((yield* scanned(scan.scan())).parents.get(PARENT)).toEqual({ type: "gone" });
      world.shells.delete(PARENT);
      expect((yield* scanned(scan.scan())).parents.get(PARENT)).toEqual({ type: "gone" });
      expect(world.sent).toHaveLength(0);
    }),
  );

  it.effect("sends nothing when the parent's tasks cannot be read", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.followed.set(PARENT, null);
      const outcome = yield* scanned((yield* service(world)).scan());
      expect(outcome.parents.get(PARENT)).toEqual({ type: "unreadable" });
      expect(world.sent).toHaveLength(0);
    }),
  );

  it.effect("is off while waits are off or Linked environments is off", () =>
    Effect.gen(function* () {
      yield* setClock;
      const off = makeWorld({ enabled: false });
      expect((yield* scanned((yield* service(off)).scan())).type).toBe("off");
      expect(off.pageReads).toHaveLength(0);
      const unlinked = makeWorld();
      unlinked.linksOff.value = true;
      expect((yield* scanned((yield* service(unlinked)).scan())).type).toBe("off");
      expect(unlinked.sent).toHaveLength(0);
    }),
  );
});

describe("DelegatedOrphanScan.run", () => {
  it.effect("scans after the delay, and again while an environment does not answer", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.there.set(ENV_C, [{ threadId: "lost-c" }]);
      world.silent.add(ENV_C);
      const scan = yield* service(world);
      const fiber = yield* scan.run().pipe(Effect.forkChild);
      yield* settle;
      expect(world.pageReads).toHaveLength(0);
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.pageTimeout);
      yield* settle;
      // The first scan reminded what env-b showed; env-c did not answer.
      expect(world.sent).toHaveLength(1);
      expect(world.sent[0]!.text).not.toContain("lost-c");
      world.silent.delete(ENV_C);
      yield* TestClock.adjust(OPTIONS.retryDelay);
      yield* settle;
      // The next scan read env-c: one new message names both.
      expect(world.sent).toHaveLength(2);
      expect(world.sent[1]!.text).toContain("lost-c");
      expect(world.sent[1]!.text).toContain("lost-1");
      // Nothing left to wait for: no third scan.
      yield* Fiber.join(fiber);
      const reads = world.pageReads.length;
      yield* TestClock.adjust(OPTIONS.retryDelay);
      yield* settle;
      expect(world.pageReads).toHaveLength(reads);
    }),
  );

  it.effect("scans again while the parent is busy, and stops after its attempts", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.shells.set(
        PARENT,
        idleShell(PARENT, { status: "running", activityRunStatus: "running" }),
      );
      const scan = yield* service(world);
      const fiber = yield* scan.run().pipe(Effect.forkChild);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      for (let attempt = 1; attempt < OPTIONS.attempts; attempt += 1) {
        yield* TestClock.adjust(OPTIONS.retryDelay);
        yield* settle;
      }
      yield* Fiber.join(fiber);
      expect(world.pageReads).toHaveLength(OPTIONS.attempts);
      expect(world.sent).toHaveLength(0);
    }),
  );
});
