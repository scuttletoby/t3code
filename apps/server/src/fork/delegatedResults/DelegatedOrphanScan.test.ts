/**
 * Fork (issue 625): the startup orphan scan. State: reruns, restarts, late machines and a moved
 * window name each orphan once; a stop half-way; the first scan's format; environments that do
 * not answer (fully); busy, stopped or gone parents; threads made after start; a held start.
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
import * as Logger from "effect/Logger";
import * as Option from "effect/Option";
import { TestClock } from "effect/testing";

import * as ServerEnvironment from "../../environment/ServerEnvironment.ts";
import * as McpToolAccessTestkit from "../../mcp/McpToolAccess.testkit.ts";
import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../../peer/PeerForwarding.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import * as WorkAdmission from "../../workAdmission.ts";
import { legacyOrphanSetId, orphanBatchIds } from "./delegatedResults.ts";
import { at, makeSlots, NOW, PARENT } from "./delegatedResults.testkit.ts";
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
  /** A minute ago by default. */
  readonly updatedAt?: number;
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
  /** Answers its first page only. */
  const cutShort = new Set<EnvironmentId>();
  const linksOff = { value: false };
  const pageReads: Array<{ environmentId: EnvironmentId; cursor: number }> = [];
  const sent: Array<{ threadId: ThreadId; messageId: string; text: string }> = [];
  const receipts = new Map<string, ThreadId>();
  // The agent slot gate, which lets every reminder through unless a test fills it.
  const slots = makeSlots();
  const page = (environmentId: EnvironmentId, cursor: number) => {
    const all = there.get(environmentId) ?? [];
    // Two threads a page, newest first, as `t3_thread_list` orders them.
    const threads = all.slice(cursor, cursor + 2).map((thread, index) => ({
      threadId: ThreadId.make(thread.threadId),
      title: `title ${thread.threadId}`,
      delegatedFrom: { ...(thread.from ?? { environmentId: HERE, threadId: PARENT }), title: "p" },
      createdAt: iso(thread.createdAt ?? NOW - 60 * 60_000),
      updatedAt: iso(thread.updatedAt ?? NOW - (cursor + index) * 60_000),
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
    cutShort,
    linksOff,
    pageReads,
    sent,
    receipts,
    slots,
    layer: Layer.mergeAll(
      ServerSettingsService.layerTest({ waitFor }),
      slots.layer,
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
        // As the real read: the thread's message ids and its accepted receipts' command ids.
        sentIds: (threadId, prefix) =>
          Effect.succeed(
            [
              ...sent
                .filter((message) => message.threadId === threadId)
                .map((message) => message.messageId),
              ...[...receipts.entries()]
                .filter(([, thread]) => thread === threadId)
                .map(([commandId]) => commandId),
            ].filter((id) => id.startsWith(prefix)),
          ),
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
            return silent.has(environmentId) || (cutShort.has(environmentId) && params.cursor > 0)
              ? Effect.never
              : Effect.succeed(page(environmentId, params.cursor));
          })) as never,
      }),
    ),
  };
};

const service = (world: ReturnType<typeof makeWorld>, options: Partial<typeof OPTIONS> = {}) =>
  DelegatedOrphanScan.make({ ...OPTIONS, ...options }).pipe(Effect.provide(world.layer));

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
        // Launched after this start (beyond the clock tolerance): its record may still be written.
        { threadId: "new-1", createdAt: NOW + DelegatedOrphanScan.CLOCK_SKEW_MS + 1 },
        { threadId: "new-2", createdAt: NOW + DelegatedOrphanScan.CLOCK_SKEW_MS },
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

  it.effect("names a thread there whose clock runs a little ahead of this one", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      // Made just before this start, stamped 30 s after it by a clock ahead of this one.
      world.there.set(ENV_B, [{ threadId: "skewed", createdAt: NOW + 30_000 }]);
      const outcome = yield* scanned((yield* service(world)).scan());
      expect(outcome.parents.get(PARENT)).toEqual({ type: "sent", threadIds: ["skewed"] });
    }),
  );

  it.effect("queues the message for a busy parent, one waiting on a question too", () =>
    Effect.gen(function* () {
      yield* setClock;
      for (const busy of [
        { status: "running", activityRunStatus: "running" },
        { pendingRuntimeRequest: {} as never },
      ] as const) {
        const world = makeWorld();
        world.shells.set(PARENT, idleShell(PARENT, busy));
        const outcome = yield* scanned((yield* service(world)).scan());
        // Sent now: it waits in the queue until the parent's run is over.
        expect(outcome.parents.get(PARENT)).toEqual({ type: "sent", threadIds: ["lost-1"] });
        expect(world.sent).toHaveLength(1);
      }
    }),
  );

  it.effect("never writes to a stopped, archived or deleted parent", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const scan = yield* service(world);
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

  it.effect("names only the new orphans after a restart, when the day window has moved", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.there.set(ENV_B, [{ threadId: "lost-1" }, { threadId: "lost-2" }]);
      yield* scanned((yield* service(world)).scan());
      expect(world.sent).toHaveLength(1);
      // A day later: lost-2 changed since (still in the window), lost-1 fell out of it (paging
      // stops before it), and a new one appeared. The next start names only the new one.
      const later = NOW + 24 * 60 * 60_000 + 60 * 60_000;
      yield* TestClock.setTime(later);
      world.followed.set(PARENT, [
        { environmentId: ENV_B, threadId: ThreadId.make("known-1") },
        { environmentId: ENV_B, threadId: ThreadId.make("known-2") },
      ]);
      world.there.set(ENV_B, [
        { threadId: "lost-3", updatedAt: later - 60_000 },
        { threadId: "lost-2", updatedAt: later - 2 * 60_000 },
        { threadId: "known-1", updatedAt: NOW - 60_000 },
        { threadId: "known-2", updatedAt: NOW - 2 * 60_000 },
        { threadId: "lost-1", updatedAt: NOW - 3 * 60_000 },
      ]);
      const reads = world.pageReads.length;
      const outcome = yield* scanned((yield* service(world)).scan());
      expect(outcome.parents.get(PARENT)).toEqual({ type: "sent", threadIds: ["lost-3"] });
      expect(world.sent).toHaveLength(2);
      expect(world.sent[1]!.text).toContain("lost-3");
      expect(world.sent[1]!.text).not.toContain("lost-2");
      // lost-1's page was never read.
      expect(world.pageReads.slice(reads).map((read) => read.cursor)).toEqual([0, 2]);
    }),
  );

  it.effect("after a stop half-way through its messages, sends only the ones not sent", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const orphans = Array.from({ length: 12 }, (_, index) => ({
        threadId: `lost-${String(index).padStart(2, "0")}`,
      }));
      world.there.set(ENV_B, orphans);
      world.followed.set(PARENT, []);
      // The last start sent the first message (ten orphans) and stopped before the second.
      const first = orphanBatchIds(
        PARENT,
        orphans.slice(0, 10).map((orphan) => ({
          environmentId: ENV_B,
          threadId: ThreadId.make(orphan.threadId),
        })),
      );
      world.sent.push({ threadId: PARENT, messageId: first.messageId, text: "" });
      world.receipts.set(first.commandId, PARENT);
      // Two threads a page: every page read.
      const allPages = { maxPages: 10 };
      const outcome = yield* scanned((yield* service(world, allPages)).scan());
      expect(outcome.parents.get(PARENT)).toEqual({
        type: "sent",
        threadIds: ["lost-10", "lost-11"],
      });
      expect(world.sent).toHaveLength(2);
      // Only the receipt survived (no message row): it still counts as sent.
      world.sent.splice(0, 1);
      expect(
        (yield* scanned((yield* service(world, allPages)).scan())).parents.get(PARENT),
      ).toEqual({ type: "none" });
      expect(world.sent).toHaveLength(1);
    }),
  );

  it.effect("takes the first scan's message for the same orphans as sent", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      const old = legacyOrphanSetId(PARENT, [
        { environmentId: ENV_B, threadId: ThreadId.make("lost-1") },
      ]);
      world.sent.push({ threadId: PARENT, messageId: old, text: "" });
      world.receipts.set(`${old}:0`, PARENT);
      const outcome = yield* scanned((yield* service(world)).scan());
      expect(outcome.parents.get(PARENT)).toEqual({ type: "none" });
      expect(world.sent).toHaveLength(1);
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
      // The next scan read env-c: one new message names only what was not named yet.
      expect(world.sent).toHaveLength(2);
      expect(world.sent[1]!.text).toContain("lost-c");
      expect(world.sent[1]!.text).not.toContain("lost-1");
      // Nothing left to wait for: no third scan.
      yield* Fiber.join(fiber);
      const reads = world.pageReads.length;
      yield* TestClock.adjust(OPTIONS.retryDelay);
      yield* settle;
      expect(world.pageReads).toHaveLength(reads);
    }),
  );

  it.effect("needs no second scan for a busy parent with no orphan", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.followed.set(PARENT, [
        { environmentId: ENV_B, threadId: ThreadId.make("known-1") },
        { environmentId: ENV_B, threadId: ThreadId.make("lost-1") },
      ]);
      world.shells.set(
        PARENT,
        idleShell(PARENT, { status: "running", activityRunStatus: "running" }),
      );
      const fiber = yield* (yield* service(world)).run().pipe(Effect.forkChild);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      yield* Fiber.join(fiber);
      expect(world.pageReads).toHaveLength(1);
    }),
  );

  it.effect("scans again while a page or a parent is unread, then logs what is left", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      // env-c answers its first page, never its second. Another parent's tasks cannot be read.
      world.there.set(ENV_C, [{ threadId: "c-1" }, { threadId: "c-2" }, { threadId: "c-3" }]);
      world.cutShort.add(ENV_C);
      const other = ThreadId.make("thread-other");
      world.there.get(ENV_B)!.push({
        threadId: "other-1",
        from: { environmentId: HERE, threadId: other },
      });
      world.followed.set(other, null);
      const warnings: Array<string> = [];
      const logger = Logger.make(({ message, logLevel }) => {
        if (logLevel === "Warn") warnings.push(JSON.stringify(message));
      });
      const scan = yield* service(world);
      const fiber = yield* scan
        .run()
        .pipe(
          Effect.provide(Logger.layer([logger], { mergeWithExisting: false })),
          Effect.forkChild,
        );
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      for (let attempt = 1; attempt <= OPTIONS.attempts; attempt += 1) {
        yield* settle;
        yield* TestClock.adjust(OPTIONS.pageTimeout);
        yield* settle;
        if (attempt < OPTIONS.attempts) yield* TestClock.adjust(OPTIONS.retryDelay);
      }
      yield* Fiber.join(fiber);
      // Each scan read env-c's first page again, and its second did not answer.
      expect(
        world.pageReads.filter((read) => read.environmentId === ENV_C && read.cursor === 0),
      ).toHaveLength(OPTIONS.attempts);
      const left = warnings.filter((warning) => warning.includes("work unchecked"));
      expect(left).toHaveLength(1);
      expect(left[0]).toContain(ENV_C);
      expect(left[0]).toContain(other);
      // A page that did not answer is not called the page limit.
      expect(warnings.some((warning) => warning.includes("page limit"))).toBe(false);
      // What env-c's first page showed was named once, over all the scans.
      expect(world.sent).toHaveLength(1);
      expect(world.sent[0]!.text).toContain("c-1");
    }),
  );

  it.effect("scans again while an environment answers only its first page", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.there.set(ENV_C, [{ threadId: "c-1" }, { threadId: "c-2" }, { threadId: "c-3" }]);
      world.cutShort.add(ENV_C);
      const fiber = yield* (yield* service(world)).run().pipe(Effect.forkChild);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.pageTimeout);
      yield* settle;
      // env-c answers in full now: the next scan reads its second page and names c-3 alone.
      world.cutShort.delete(ENV_C);
      yield* TestClock.adjust(OPTIONS.retryDelay);
      yield* settle;
      yield* Fiber.join(fiber);
      expect(world.pageReads.filter((read) => read.environmentId === ENV_C)).toHaveLength(4);
      expect(world.sent).toHaveLength(2);
      expect(world.sent[1]!.text).toContain("c-3");
      expect(world.sent[1]!.text).not.toContain("c-1");
    }),
  );
});

describe("DelegatedOrphanScan.layerWith", () => {
  const started = (heldFromBoot: boolean) =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      yield* Effect.scoped(
        Effect.gen(function* () {
          yield* Layer.build(
            DelegatedOrphanScan.layerWith(OPTIONS).pipe(
              Layer.provide(
                Layer.merge(
                  world.layer,
                  Layer.succeed(WorkAdmission.WorkAdmission, { heldFromBoot } as never),
                ),
              ),
            ),
          );
          yield* settle;
          yield* TestClock.adjust(OPTIONS.delay);
          yield* settle;
        }),
      );
      return world;
    });

  it.effect("scans after a normal start, never after a held one", () =>
    Effect.gen(function* () {
      expect((yield* started(false)).sent).toHaveLength(1);
      const held = yield* started(true);
      expect(held.pageReads).toHaveLength(0);
      expect(held.sent).toHaveLength(0);
    }),
  );
});

/**
 * Fork (issue 711): an orphan reminder starts a run in an idle parent, so it asks the agent slot
 * gate. A parent refused waits for a slot alone (no rescan), asked again every few seconds for as
 * long as it takes; it is sent once a slot frees, never twice, and found again after a restart.
 */
describe("DelegatedOrphanScan and agent slots", () => {
  const SLOT_RETRY = Duration.seconds(5);

  it.effect("asks the gate as a wake-up of the parent; a refusal needs no rescan", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.slots.state.all = true;
      const scan = yield* service(world);
      const outcome = yield* scanned(scan.scan());
      expect(outcome.parents.get(PARENT)).toMatchObject({ type: "waitsForSlot" });
      expect(world.sent).toHaveLength(0);
      expect(world.slots.asks).toEqual([
        expect.objectContaining({
          callerThreadId: PARENT,
          intoThreadId: PARENT,
          keepsHeld: true,
        }),
      ]);
    }),
  );

  it.effect("waits for a slot past every attempt, logs it once, then sends once and ends", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.slots.state.all = true;
      const infos: Array<string> = [];
      const logger = Logger.make(({ message, logLevel }) => {
        if (logLevel === "Info") infos.push(JSON.stringify(message));
      });
      const scan = yield* service(world);
      const fiber = yield* scan
        .run()
        .pipe(
          Effect.provide(Logger.layer([logger], { mergeWithExisting: false })),
          Effect.forkChild,
        );
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      expect(world.slots.asks).toHaveLength(1);
      // Far more tries than `attempts` (3), every 5 seconds, and none of them a new scan.
      for (let step = 0; step < 24; step++) {
        yield* TestClock.adjust(SLOT_RETRY);
        yield* settle;
      }
      expect(world.slots.asks).toHaveLength(25);
      expect(world.pageReads).toHaveLength(1);
      expect(world.sent).toHaveLength(0);
      expect(infos.filter((info) => info.includes("waits for an agent slot"))).toHaveLength(1);
      world.slots.state.all = false;
      yield* TestClock.adjust(SLOT_RETRY);
      yield* settle;
      expect(world.sent).toHaveLength(1);
      expect(world.sent[0]!.text).toContain("lost-1");
      // The run ends with its last wait: nothing more is asked or sent.
      yield* Fiber.join(fiber);
      yield* TestClock.adjust(Duration.hours(1));
      yield* settle;
      expect(world.slots.asks).toHaveLength(26);
      expect(world.sent).toHaveLength(1);
    }),
  );

  it.effect("a parent waiting for a slot is left to its wait by the next scan", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      // env-c does not answer, so the run scans again while the parent still waits.
      world.there.set(ENV_C, [
        { threadId: "lost-c", from: { environmentId: HERE, threadId: PARENT } },
      ]);
      world.silent.add(ENV_C);
      world.slots.state.all = true;
      const scan = yield* service(world);
      const fiber = yield* scan.run().pipe(Effect.forkChild);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.pageTimeout);
      yield* settle;
      expect(world.slots.asks).toHaveLength(1);
      // Past the second scan (10 minutes on, plus env-c's page timeout): one ask per 5 seconds,
      // none from the scan, which leaves the parent to its wait.
      const steps =
        (Duration.toMillis(OPTIONS.retryDelay) + Duration.toMillis(OPTIONS.pageTimeout)) / 5_000;
      for (let step = 0; step < steps; step++) {
        yield* TestClock.adjust(SLOT_RETRY);
        yield* settle;
      }
      expect(world.pageReads.filter((read) => read.environmentId === ENV_C)).toHaveLength(2);
      expect(world.slots.asks).toHaveLength(1 + steps);
      world.slots.state.all = false;
      world.silent.delete(ENV_C);
      yield* TestClock.adjust(SLOT_RETRY);
      yield* settle;
      // The wait's list, then a fresh one: env-c's orphan, unread by the scans, is named too.
      expect(world.sent.map((message) => message.text.includes("lost-c"))).toEqual([false, true]);
      yield* Fiber.interrupt(fiber);
    }),
  );

  it.effect("an orphan found while its parent waits is named right after, from a fresh list", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.slots.state.all = true;
      const fiber = yield* (yield* service(world)).run().pipe(Effect.forkChild);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      // Not in the list the first scan read: it shows only now.
      world.there.get(ENV_B)!.push({ threadId: "lost-2" });
      yield* TestClock.adjust(SLOT_RETRY);
      yield* settle;
      world.slots.state.all = false;
      yield* TestClock.adjust(SLOT_RETRY);
      yield* settle;
      yield* Fiber.join(fiber);
      expect(world.sent.map((message) => message.text.includes("lost-2"))).toEqual([false, true]);
      expect(world.sent[0]!.text).toContain("lost-1");
    }),
  );

  it.effect(
    "a parent that stays unreadable: logged once, left to the next start after 3 tries",
    () =>
      Effect.gen(function* () {
        yield* setClock;
        const world = makeWorld();
        world.slots.state.all = true;
        const warnings: Array<string> = [];
        const logger = Logger.make(({ message, logLevel }) => {
          if (logLevel === "Warn") warnings.push(JSON.stringify(message));
        });
        const fiber = yield* (yield* service(world))
          .run()
          .pipe(
            Effect.provide(Logger.layer([logger], { mergeWithExisting: false })),
            Effect.forkChild,
          );
        yield* settle;
        yield* TestClock.adjust(OPTIONS.delay);
        yield* settle;
        // Its tasks can no longer be read (a broken store or linked machine).
        world.followed.set(PARENT, null);
        yield* TestClock.adjust(SLOT_RETRY);
        yield* settle;
        for (let step = 0; step < OPTIONS.attempts; step++) {
          yield* TestClock.adjust(OPTIONS.retryDelay);
          yield* settle;
        }
        expect(fiber.pollUnsafe()).toBeDefined();
        expect(warnings.filter((warning) => warning.includes("cannot be read"))).toHaveLength(1);
        expect(
          warnings.filter((warning) => warning.includes("left to the next start")),
        ).toHaveLength(1);
        expect(world.sent).toHaveLength(0);
        // The next start finds it again once it can be read.
        world.followed.set(PARENT, [{ environmentId: ENV_B, threadId: ThreadId.make("known-1") }]);
        world.slots.state.all = false;
        const again = yield* (yield* service(world)).run().pipe(Effect.forkChild);
        yield* settle;
        yield* TestClock.adjust(OPTIONS.delay);
        yield* settle;
        yield* Fiber.join(again);
        expect(world.sent).toHaveLength(1);
      }),
  );

  it.effect("a restart while one waits loses nothing: the next start names it once", () =>
    Effect.gen(function* () {
      yield* setClock;
      const world = makeWorld();
      world.slots.state.all = true;
      const fiber = yield* (yield* service(world)).run().pipe(Effect.forkChild);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      yield* TestClock.adjust(SLOT_RETRY);
      yield* settle;
      expect(world.slots.asks).toHaveLength(2);
      // Stopped half-way through its wait.
      yield* Fiber.interrupt(fiber);
      world.slots.state.all = false;
      const again = yield* (yield* service(world)).run().pipe(Effect.forkChild);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      yield* Fiber.join(again);
      expect(world.sent).toHaveLength(1);
      expect(world.sent[0]!.text).toContain("lost-1");
      // Runs again after that: never a second message.
      const third = yield* (yield* service(world)).run().pipe(Effect.forkChild);
      yield* settle;
      yield* TestClock.adjust(OPTIONS.delay);
      yield* settle;
      yield* Fiber.join(third);
      expect(world.sent).toHaveLength(1);
    }),
  );
});
