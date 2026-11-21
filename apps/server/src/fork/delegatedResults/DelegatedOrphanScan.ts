/**
 * Fork: names each thread there that no task here follows, made before the start, to its parent
 * once (one id key per orphan, so reruns and restarts never repeat one); drop once upstream records
 * before launch. A reminder takes an agent slot; refused, it is asked again and never given up.
 */
import type { EnvironmentId, ThreadId } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";

import * as ServerEnvironment from "../../environment/ServerEnvironment.ts";
import type { McpInvocationScope } from "../../mcp/McpInvocationContext.ts";
import { OrchestratorToolkit } from "../../mcp/toolkits/orchestrator/tools.ts";
import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../../peer/PeerForwarding.ts";
import { forkParked } from "../../serverActivation.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import * as SlotGate from "../../slots/SlotGate.ts";
import * as WaitDelivery from "../../waits/WaitDelivery.ts";
import { WAKE_SLOT_RETRY_MS } from "../../waits/WaitService.ts";
import * as WorkAdmission from "../../workAdmission.ts";
import {
  type LinkedThread,
  ORPHAN_ID_PREFIX,
  orphanBatches,
  orphanBatchIds,
  orphanReminderText,
  orphansOf,
  makeRefusalLog,
  parentGone,
  REMINDER_MAX_AGE_MS,
} from "./delegatedResults.ts";
import * as DelegatedResultsStore from "./DelegatedResultsStore.ts";
import { LINKED_PAGE_SIZE, listLinkedThreads } from "./linkedThreads.ts";

export interface OrphanScanOptions {
  /** From the start to the first scan: links come back and followers settle first. */
  readonly delay: Duration.Input;
  /** Between scans while an environment did not answer (fully) or a parent could not be read. */
  readonly retryDelay: Duration.Input;
  /** Scans in all, the first one included. */
  readonly attempts: number;
  /** Pages of `t3_thread_list` read per environment and scan. */
  readonly maxPages: number;
  /** The longest one page may take before the environment counts as not answering. */
  readonly pageTimeout: Duration.Input;
  /**
   * Between tries of one parent while the agent slot gate refuses (the wait service's
   * `WAKE_SLOT_RETRY_MS` by default). Not counted in `attempts`, and no rescan: only that
   * parent's check runs again.
   */
  readonly slotRetryDelay?: Duration.Input;
}

/**
 * A thread there counts as made before this start up to this long after it: the other machine's
 * clock may run ahead of this one. A launch in that minute was recorded long before the first
 * scan (`delay`), so it is never raced; only a launch whose record failed (the agent saw an error
 * then) may be named too, and the message's wording covers that.
 */
export const CLOCK_SKEW_MS = 60_000;

const DEFAULT_OPTIONS: OrphanScanOptions = {
  delay: Duration.minutes(3),
  retryDelay: Duration.minutes(10),
  attempts: 6,
  maxPages: 20,
  pageTimeout: Duration.seconds(10),
};

/** What one parent's check did; tests read it. */
export type ParentOutcome =
  /** No orphan, or every one was named before. */
  | { readonly type: "none" }
  | { readonly type: "gone" }
  /** The parent could not be read, or (`sendFailed`, why) a reminder's send failed: tried again. */
  | { readonly type: "unreadable"; readonly sendFailed?: string }
  /**
   * The agent slot gate refused (nothing of that message was sent): `run` asks again for this
   * parent alone, with the threads this scan found.
   */
  | {
      readonly type: "waitsForSlot";
      readonly detail: string;
      readonly linked: ReadonlyArray<LinkedThread>;
    }
  /** Named now; a busy parent reads it after its current run (an open question included). */
  | { readonly type: "sent"; readonly threadIds: ReadonlyArray<ThreadId> }
  | { readonly type: "held"; readonly detail: string };

/** What one scan did; tests read it. */
export interface ScanOutcome {
  readonly type: "off" | "scanned";
  /** Environments whose first page did not answer: scanned again. */
  readonly unreachable: ReadonlyArray<EnvironmentId>;
  /** Environments whose later page did not answer: scanned again. */
  readonly cutShort: ReadonlyArray<EnvironmentId>;
  /** Environments with more matching threads than the page limit reads (a rerun reads the same). */
  readonly partial: ReadonlyArray<EnvironmentId>;
  readonly parents: ReadonlyMap<ThreadId, ParentOutcome>;
}

const OFF: ScanOutcome = {
  type: "off",
  unreachable: [],
  cutShort: [],
  partial: [],
  parents: new Map(),
};

export const make = Effect.fn("DelegatedOrphanScan.make")(function* (
  options: OrphanScanOptions = DEFAULT_OPTIONS,
) {
  const threads = yield* ThreadManagement.ThreadManagementService;
  // The server's one gate (`SlotGate.layer` memoizes): none in a test that gives none.
  const delivery = yield* WaitDelivery.make(yield* Effect.serviceOption(SlotGate.SlotGate));
  const slotRetryDelay = options.slotRetryDelay ?? Duration.millis(WAKE_SLOT_RETRY_MS);
  /** Parents whose own retry runs (`run`): a later scan leaves them to it. */
  const waitingForSlot = new Map<ThreadId, ParentOutcome>();
  // Never forgotten: an orphan batch's ids do not age, and a slot wait checks its parent again for
  // as long as the slot stays taken, a day or more. One run per start, so this holds at most the
  // refused batches of that run.
  const newRefusal = makeRefusalLog(null);
  const settings = yield* ServerSettingsService;
  const store = yield* DelegatedResultsStore.DelegatedResultsStore;
  const forwarding = yield* PeerForwarding.PeerForwarding;
  const hereId = yield* ServerEnvironment.ServerEnvironment.pipe(
    Effect.flatMap((environment) => environment.getEnvironmentId),
  );
  // Only a launch before this start can have lost its record: a later one is recorded by this
  // server, or found by the scan after the next start.
  const startedAtMs = yield* Clock.currentTimeMillis;
  // The server itself asks, not an agent: no thread, no client.
  const scope: McpInvocationScope = {
    environmentId: hereId,
    requestNamespace: "delegated-orphan-scan",
    thread: undefined,
    client: undefined,
    capabilities: new Set(["orchestration"]),
    issuedAt: 0,
  };

  const checkParent = Effect.fn("DelegatedOrphanScan.checkParent")(function* (
    parentThreadId: ThreadId,
    linked: ReadonlyArray<LinkedThread>,
  ) {
    // The orphans first: a parent with none never needs its shell, nor another scan.
    const followed = yield* store.remoteChildrenOf(parentThreadId);
    if (followed === null) return { type: "unreadable" } satisfies ParentOutcome;
    const orphans = orphansOf(linked, hereId, parentThreadId, followed);
    if (orphans.length === 0) return { type: "none" } satisfies ParentOutcome;
    const sentIds = yield* store.sentIds(parentThreadId, ORPHAN_ID_PREFIX);
    if (sentIds === null) return { type: "unreadable" } satisfies ParentOutcome;
    const batches = orphanBatches(parentThreadId, orphans, sentIds);
    if (batches.length === 0) return { type: "none" } satisfies ParentOutcome;
    const shell = yield* threads
      .getThreadShell(parentThreadId)
      .pipe(Effect.orElseSucceed(() => undefined));
    if (shell === undefined) return { type: "unreadable" } satisfies ParentOutcome;
    if (parentGone(shell)) return { type: "gone" } satisfies ParentOutcome;
    // A busy parent is not held: its run's end changes nothing about an orphan, and the message
    // waits in its queue until that run (a question to a person included) is over.
    const sent: Array<ThreadId> = [];
    let sendFailed: string | undefined;
    let refused: string | undefined;
    for (const batch of batches) {
      const ids = orphanBatchIds(parentThreadId, batch);
      const result = yield* delivery.deliver({
        threadId: parentThreadId,
        messageId: ids.messageId,
        commandId: ids.commandId,
        text: orphanReminderText(batch),
      });
      switch (result.type) {
        case "delivered":
          sent.push(...batch.map((orphan) => orphan.threadId));
          break;
        case "gone":
          return { type: "gone" } satisfies ParentOutcome;
        case "stopped":
          // A person stopped it: never restart it from here.
          return { type: "held", detail: "stopped" } satisfies ParentOutcome;
        case "rejected":
          // Logged once per message and process, not on each pass that finds its refused receipt.
          if (newRefusal(ids.commandId, yield* Clock.currentTimeMillis)) {
            yield* Effect.logWarning("delegated orphan reminder refused", {
              threadId: parentThreadId,
              commandId: ids.commandId,
              detail: result.detail,
            });
          }
          refused = result.detail;
          break;
        case "retry":
          sendFailed = result.detail;
          break;
        case "waitsForSlot":
          // The next batches go to the same idle thread: they wait too. Sent ones are recorded.
          return { type: "waitsForSlot", detail: result.detail, linked } satisfies ParentOutcome;
      }
    }
    if (sendFailed !== undefined) return { type: "unreadable", sendFailed } satisfies ParentOutcome;
    if (refused !== undefined) return { type: "held", detail: refused } satisfies ParentOutcome;
    return { type: "sent", threadIds: sent } satisfies ParentOutcome;
  });

  /** Every linked environment's threads made from here, by parent; `null` while off. */
  const listByParent = Effect.fn("DelegatedOrphanScan.listByParent")(function* () {
    const enabled = yield* settings.getSettings.pipe(
      Effect.map((current) => current.waitFor.enabled),
      Effect.orElseSucceed(() => false),
    );
    if (!enabled) return null;
    // Fails while Linked environments is off here.
    const links = yield* forwarding.links(scope).pipe(Effect.orElseSucceed(() => null));
    if (links === null) return null;
    const sinceMs = startedAtMs - REMINDER_MAX_AGE_MS;
    const unreachable: Array<EnvironmentId> = [];
    const cutShort: Array<EnvironmentId> = [];
    const partial: Array<EnvironmentId> = [];
    const byParent = new Map<ThreadId, Array<LinkedThread>>();
    yield* Effect.forEach(
      links.links.filter((link) => link.status !== "expired"),
      (link) =>
        listLinkedThreads(
          (cursor) =>
            forwarding.call(scope, OrchestratorToolkit.tools.t3_thread_list, link.environmentId, {
              allProjects: true,
              limit: LINKED_PAGE_SIZE,
              cursor,
            }),
          link.environmentId,
          {
            maxPages: options.maxPages,
            timeout: options.pageTimeout,
            sinceMs,
            createdBeforeMs: startedAtMs + CLOCK_SKEW_MS,
          },
        ).pipe(
          Effect.map((read) => {
            if (read.state === "unreachable") unreachable.push(link.environmentId);
            if (read.state === "cut-short") cutShort.push(link.environmentId);
            if (read.state === "limit") partial.push(link.environmentId);
            for (const thread of read.threads) {
              if (thread.delegatedFrom.environmentId !== hereId) continue;
              const parent = thread.delegatedFrom.threadId;
              byParent.set(parent, [...(byParent.get(parent) ?? []), thread]);
            }
          }),
        ),
      { concurrency: 4, discard: true },
    );
    return { byParent, unreachable, cutShort, partial };
  });

  /** One pass over every linked environment; a parent is checked once, over all of them. */
  const scan = Effect.fn("DelegatedOrphanScan.scan")(function* () {
    const listed = yield* listByParent();
    if (listed === null) return OFF;
    const { byParent, unreachable, cutShort, partial } = listed;
    const parents = new Map<ThreadId, ParentOutcome>();
    for (const [parentThreadId, linked] of byParent) {
      const waiting = waitingForSlot.get(parentThreadId);
      parents.set(parentThreadId, waiting ?? (yield* checkParent(parentThreadId, linked)));
    }
    if (partial.length > 0) {
      yield* Effect.logWarning(
        "delegated orphan scan read only the newest threads: the page limit was reached",
        { environmentIds: partial, threads: options.maxPages * LINKED_PAGE_SIZE },
      );
    }
    return { type: "scanned", unreachable, cutShort, partial, parents } satisfies ScanOutcome;
  });

  /** Parents tried again: unreadable ones, or (`sendFailed`) ones whose reminder's send failed. */
  const unreadableParents = (outcome: ScanOutcome, sendFailed?: boolean) =>
    [...outcome.parents.entries()]
      .filter(
        ([, parent]) =>
          parent.type === "unreadable" &&
          (sendFailed === undefined || (parent.sendFailed !== undefined) === sendFailed),
      )
      .map(([threadId]) => threadId);

  /** Another pass while an environment did not answer (fully) or a parent could not be read. */
  const needsAnother = (outcome: ScanOutcome): boolean =>
    outcome.type === "scanned" &&
    (outcome.unreachable.length > 0 ||
      outcome.cutShort.length > 0 ||
      unreadableParents(outcome).length > 0);

  /**
   * One parent refused a slot: checked again every `slotRetryDelay` while refused, and every
   * `retryDelay` while unreadable, `attempts` times in a row at most (then the next start). Once
   * through, listed afresh: an orphan that ended meanwhile is named too. A restart loses nothing.
   */
  const untilSlot = Effect.fn("DelegatedOrphanScan.untilSlot")(
    function* (parentThreadId: ThreadId, first: ParentOutcome & { type: "waitsForSlot" }) {
      yield* Effect.logInfo("delegated orphan reminder waits for an agent slot", {
        threadId: parentThreadId,
        detail: first.detail,
      });
      let linked = first.linked;
      let outcome: ParentOutcome = first;
      let relisted = false;
      let unreadable = 0;
      while (true) {
        if ((outcome.type === "sent" || outcome.type === "none") && !relisted) {
          relisted = true;
          const fresh = (yield* listByParent())?.byParent.get(parentThreadId) ?? [];
          const freshKeys = new Set(fresh.map((thread) => thread.threadId));
          linked = [...fresh, ...linked.filter((thread) => !freshKeys.has(thread.threadId))];
          outcome = yield* checkParent(parentThreadId, linked);
          continue;
        }
        if (outcome.type !== "waitsForSlot" && outcome.type !== "unreadable") return;
        unreadable = outcome.type === "unreadable" ? unreadable + 1 : 0;
        // A failed send is told as one, not as a parent that cannot be read.
        const sendFailed = outcome.type === "unreadable" ? outcome.sendFailed : undefined;
        if (unreadable === 1)
          yield* sendFailed === undefined
            ? Effect.logWarning("delegated orphan reminder: its parent cannot be read", {
                threadId: parentThreadId,
              })
            : Effect.logWarning("delegated orphan reminder: its send failed", {
                threadId: parentThreadId,
                detail: sendFailed,
              });
        if (unreadable >= options.attempts)
          return yield* Effect.logWarning(
            sendFailed === undefined
              ? "delegated orphan reminder left to the next start: its parent stayed unreadable"
              : "delegated orphan reminder left to the next start: its send kept failing",
            { threadId: parentThreadId, tries: unreadable },
          );
        yield* Effect.sleep(outcome.type === "waitsForSlot" ? slotRetryDelay : options.retryDelay);
        outcome = yield* checkParent(parentThreadId, linked);
      }
    },
    (effect, parentThreadId) =>
      effect.pipe(
        Effect.catchCause((cause) =>
          Effect.logWarning("delegated orphan reminder failed", {
            threadId: parentThreadId,
            cause,
          }),
        ),
        Effect.ensuring(Effect.sync(() => waitingForSlot.delete(parentThreadId))),
      ),
  );

  /**
   * Scans after the delay, and again while `needsAnother`, at most `attempts` times. What is still
   * unchecked then is logged by name; the next start scans it again (within its one-day window).
   * A parent refused a slot gets its own retry (`untilSlot`); `run` ends when those end.
   */
  const run = Effect.fn("DelegatedOrphanScan.run")(
    function* () {
      const slotWaits: Array<Fiber.Fiber<void>> = [];
      yield* Effect.sleep(options.delay);
      for (let attempt = 1; attempt <= options.attempts; attempt += 1) {
        const outcome = yield* scan();
        for (const [parentThreadId, parent] of outcome.parents) {
          if (parent.type !== "waitsForSlot" || waitingForSlot.has(parentThreadId)) continue;
          waitingForSlot.set(parentThreadId, parent);
          slotWaits.push(yield* Effect.forkChild(untilSlot(parentThreadId, parent)));
        }
        if (!needsAnother(outcome)) break;
        if (attempt < options.attempts) {
          yield* Effect.sleep(options.retryDelay);
          continue;
        }
        yield* Effect.logWarning(
          "delegated orphan scan stopped with work unchecked until the next start",
          {
            scans: options.attempts,
            unansweredEnvironmentIds: outcome.unreachable,
            cutShortEnvironmentIds: outcome.cutShort,
            unreadableParentThreadIds: unreadableParents(outcome, false),
            failedSendParentThreadIds: unreadableParents(outcome, true),
          },
        );
      }
      yield* Fiber.joinAll(slotWaits);
    },
    (effect) =>
      effect.pipe(
        Effect.catchCause((cause) => Effect.logWarning("delegated orphan scan failed", { cause })),
      ),
  );

  return { scan, run, checkParent };
});

/** A held server (`serve --hold-work`) neither follows nor reminds; its next start does. */
export const layerWith = (options: OrphanScanOptions = DEFAULT_OPTIONS) =>
  Layer.effectDiscard(
    Effect.gen(function* () {
      if ((yield* WorkAdmission.WorkAdmission).heldFromBoot) return;
      const service = yield* make(options);
      yield* forkParked(service.run());
    }),
  );

/**
 * In the routes beside upstream's follower of linked tasks (fork/forkLayers.ts), which has the
 * links.
 */
export const layer = layerWith().pipe(
  Layer.provide(
    Layer.mergeAll(
      // The server's one gate: layers memoize by reference.
      SlotGate.layer,
      CommandReceiptStore.layerFromApplicationReceipts,
      DelegatedResultsStore.layer,
    ),
  ),
);
