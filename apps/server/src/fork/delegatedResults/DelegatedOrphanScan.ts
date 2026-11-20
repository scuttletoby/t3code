/**
 * Fork (issue 625, part of 386): a stop between the linked thread's creation and the task's record
 * leaves a thread there no task here follows. At start, each such thread made before the start gets
 * its parent one queued message; ids come from the orphans, so a rerun or restart never repeats it.
 */
import type { EnvironmentId, ThreadId } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as ServerEnvironment from "../../environment/ServerEnvironment.ts";
import type { McpInvocationScope } from "../../mcp/McpInvocationContext.ts";
import { OrchestratorToolkit } from "../../mcp/toolkits/orchestrator/tools.ts";
import * as CommandReceiptStore from "../../orchestration-v2/CommandReceiptStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../../peer/PeerForwarding.ts";
import { forkParked } from "../../serverActivation.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import * as WaitDelivery from "../../waits/WaitDelivery.ts";
import * as WorkAdmission from "../../workAdmission.ts";
import {
  type LinkedThread,
  orphanReminderIds,
  orphanReminderText,
  orphansOf,
  parentBusy,
  parentGone,
  REMINDER_MAX_AGE_MS,
} from "./delegatedResults.ts";
import * as DelegatedResultsStore from "./DelegatedResultsStore.ts";
import { LINKED_PAGE_SIZE, listLinkedThreads } from "./linkedThreads.ts";

export interface OrphanScanOptions {
  /** From the start to the first scan: links come back and followers settle first. */
  readonly delay: Duration.Input;
  /** Between scans while an environment did not answer or a parent was busy. */
  readonly retryDelay: Duration.Input;
  /** Scans in all, the first one included. */
  readonly attempts: number;
  /** Pages of `t3_thread_list` read per environment and scan. */
  readonly maxPages: number;
  /** The longest one page may take before the environment counts as not answering. */
  readonly pageTimeout: Duration.Input;
}

const DEFAULT_OPTIONS: OrphanScanOptions = {
  delay: Duration.minutes(3),
  retryDelay: Duration.minutes(10),
  attempts: 6,
  maxPages: 20,
  pageTimeout: Duration.seconds(10),
};

/** What one parent's check did; tests read it. */
export type ParentOutcome =
  | { readonly type: "none" }
  | { readonly type: "busy" }
  | { readonly type: "gone" }
  | { readonly type: "unreadable" }
  | { readonly type: "sent"; readonly threadIds: ReadonlyArray<ThreadId> }
  | { readonly type: "held"; readonly detail: string };

/** What one scan did; tests read it. */
export interface ScanOutcome {
  readonly type: "off" | "scanned";
  /** Environments whose first page did not answer: scanned again. */
  readonly unreachable: ReadonlyArray<EnvironmentId>;
  /** Environments with more matching threads than the page limit reads. */
  readonly partial: ReadonlyArray<EnvironmentId>;
  readonly parents: ReadonlyMap<ThreadId, ParentOutcome>;
}

const OFF: ScanOutcome = { type: "off", unreachable: [], partial: [], parents: new Map() };

export const make = Effect.fn("DelegatedOrphanScan.make")(function* (
  options: OrphanScanOptions = DEFAULT_OPTIONS,
) {
  const threads = yield* ThreadManagement.ThreadManagementService;
  const delivery = yield* WaitDelivery.make();
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
    const shell = yield* threads
      .getThreadShell(parentThreadId)
      .pipe(Effect.orElseSucceed(() => undefined));
    if (shell === undefined) return { type: "unreadable" } satisfies ParentOutcome;
    if (parentGone(shell)) return { type: "gone" } satisfies ParentOutcome;
    if (parentBusy(shell!)) return { type: "busy" } satisfies ParentOutcome;
    const followed = yield* store.remoteChildrenOf(parentThreadId);
    if (followed === null) return { type: "unreadable" } satisfies ParentOutcome;
    const orphans = orphansOf(linked, hereId, parentThreadId, followed);
    if (orphans.length === 0) return { type: "none" } satisfies ParentOutcome;
    const ids = orphanReminderIds(parentThreadId, orphans);
    const result = yield* delivery.deliver({
      threadId: parentThreadId,
      messageId: ids.messageId,
      commandId: ids.commandId,
      text: orphanReminderText(orphans),
    });
    switch (result.type) {
      case "delivered":
        return {
          type: "sent",
          threadIds: orphans.map((orphan) => orphan.threadId),
        } satisfies ParentOutcome;
      case "gone":
        return { type: "gone" } satisfies ParentOutcome;
      case "stopped":
        // A person stopped it: never restart it from here.
        return { type: "held", detail: "stopped" } satisfies ParentOutcome;
      case "rejected":
        yield* Effect.logWarning("delegated orphan reminder refused", {
          threadId: parentThreadId,
          detail: result.detail,
        });
        return { type: "held", detail: result.detail } satisfies ParentOutcome;
      case "retry":
        return { type: "unreadable" } satisfies ParentOutcome;
    }
  });

  /** One pass over every linked environment; a parent is checked once, over all of them. */
  const scan = Effect.fn("DelegatedOrphanScan.scan")(function* () {
    const enabled = yield* settings.getSettings.pipe(
      Effect.map((current) => current.waitFor.enabled),
      Effect.orElseSucceed(() => false),
    );
    if (!enabled) return OFF;
    // Fails while Linked environments is off here.
    const links = yield* forwarding.links(scope).pipe(Effect.orElseSucceed(() => null));
    if (links === null) return OFF;
    const sinceMs = startedAtMs - REMINDER_MAX_AGE_MS;
    const unreachable: Array<EnvironmentId> = [];
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
            createdBeforeMs: startedAtMs,
          },
        ).pipe(
          Effect.map((read) => {
            if (read.state === "unreachable") unreachable.push(link.environmentId);
            if (read.state === "partial") partial.push(link.environmentId);
            for (const thread of read.threads) {
              if (thread.delegatedFrom.environmentId !== hereId) continue;
              const parent = thread.delegatedFrom.threadId;
              byParent.set(parent, [...(byParent.get(parent) ?? []), thread]);
            }
          }),
        ),
      { concurrency: 4, discard: true },
    );
    const parents = new Map<ThreadId, ParentOutcome>();
    for (const [parentThreadId, linked] of byParent) {
      parents.set(parentThreadId, yield* checkParent(parentThreadId, linked));
    }
    if (partial.length > 0) {
      yield* Effect.logWarning("delegated orphan scan read only the newest threads", {
        environmentIds: partial,
        threads: options.maxPages * LINKED_PAGE_SIZE,
      });
    }
    return { type: "scanned", unreachable, partial, parents } satisfies ScanOutcome;
  });

  /** A scan needs another pass while an environment did not answer or a parent could not take it. */
  const needsAnother = (outcome: ScanOutcome): boolean =>
    outcome.type === "scanned" &&
    (outcome.unreachable.length > 0 ||
      [...outcome.parents.values()].some(
        (parent) => parent.type === "busy" || parent.type === "unreadable",
      ));

  /** Scans after the delay, and again while `needsAnother`, at most `attempts` times. */
  const run = Effect.fn("DelegatedOrphanScan.run")(
    function* () {
      yield* Effect.sleep(options.delay);
      for (let attempt = 1; attempt <= options.attempts; attempt += 1) {
        const outcome = yield* scan();
        if (!needsAnother(outcome)) return;
        if (attempt < options.attempts) yield* Effect.sleep(options.retryDelay);
      }
    },
    (effect) =>
      effect.pipe(
        Effect.catchCause((cause) => Effect.logWarning("delegated orphan scan failed", { cause })),
      ),
  );

  return { scan, run, checkParent };
});

/**
 * In the routes beside upstream's follower of linked tasks (fork/forkLayers.ts), which has the
 * links. A held server (`serve --hold-work`) neither follows nor reminds; its next start does.
 */
export const layer = Layer.effectDiscard(
  Effect.gen(function* () {
    if ((yield* WorkAdmission.WorkAdmission).heldFromBoot) return;
    const service = yield* make();
    yield* forkParked(service.run());
  }),
).pipe(
  Layer.provide(
    Layer.merge(CommandReceiptStore.layerFromApplicationReceipts, DelegatedResultsStore.layer),
  ),
);
