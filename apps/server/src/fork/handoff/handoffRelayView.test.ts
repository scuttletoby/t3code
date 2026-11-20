/**
 * Fork: the handoff settle reads the thread as upstream's relay shows it (`handoffRelayView.ts`).
 * Each case is a state the relay would not show as "finished"; unsure counts as not finished.
 */
import { describe, expect, it } from "@effect/vitest";
import { RuntimeRequestId, ThreadId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as TestClock from "effect/testing/TestClock";

import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import type {
  ActiveShellIndexEntry,
  AgentAwarenessThread,
} from "../../orchestration-v2/ProjectionStore.ts";
import { at, awarenessShell, MINUTE_MS, NOW_MS, projection, threadId } from "./handoff.testkit.ts";
import { relayShowsFinish } from "./handoffRelayView.ts";

const base = projection({ usedTokens: 0 });
const finished = awarenessShell(base, { updatedAt: at() });

/** A child shell under `parent`, waiting on the person's approval unless `request` is false. */
const child = (id: string, parent: string, request = true): AgentAwarenessThread => ({
  ...finished,
  id: ThreadId.make(id),
  lineage: {
    rootThreadId: threadId,
    parentThreadId: ThreadId.make(parent),
    relationshipToParent: "subagent",
  },
  status: "running",
  activityRunStatus: "running",
  pendingRuntimeRequest: request
    ? { id: RuntimeRequestId.make(`request:${id}`), kind: "command", createdAt: at() }
    : null,
});

const entry = (shell: AgentAwarenessThread, mayBeLive = true): ActiveShellIndexEntry => ({
  id: shell.id,
  projectId: shell.projectId,
  lineage: shell.lineage,
  settledOverride: null,
  settledAt: null,
  pinnedAt: null,
  pinOrderKey: null,
  snoozedUntil: null,
  mayBeLive,
  hasRemoteTasks: false,
});

/** Asks with the given shells; `index` is the active shell index (defaults to every child). */
const shows = (
  own: AgentAwarenessThread | null,
  children: ReadonlyArray<AgentAwarenessThread> = [],
  index: ReadonlyArray<ActiveShellIndexEntry> = children.map((one) => entry(one)),
) =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW_MS);
    return yield* relayShowsFinish(threadId);
  }).pipe(
    Effect.provide(
      Layer.mock(Orchestrator.OrchestratorV2)({
        getAgentAwarenessSnapshot: (requested) =>
          Effect.succeed({
            snapshotSequence: 1,
            threads: [own, ...children].filter(
              (one): one is AgentAwarenessThread => one !== null && one.id === requested,
            ),
          }),
        listActiveShellIndex: () => Effect.succeed(index),
      }),
    ),
  );

describe("relayShowsFinish", () => {
  it.effect("a finished thread with nothing else going on is shown finished", () =>
    Effect.gen(function* () {
      expect(yield* shows(finished)).toBe(true);
    }),
  );

  it.effect("missing, archived or not finished: not shown (old or empty state alerts)", () =>
    Effect.gen(function* () {
      expect(yield* shows(null)).toBe(false);
      expect(yield* shows({ ...finished, archivedAt: at() })).toBe(false);
      expect(yield* shows({ ...finished, status: "running" })).toBe(false);
      const request = {
        id: RuntimeRequestId.make("own"),
        kind: "command" as const,
        createdAt: at(),
      };
      expect(yield* shows({ ...finished, pendingRuntimeRequest: request })).toBe(false);
    }),
  );

  it.effect("a snoozed thread is not shown; once the snooze ends it is", () =>
    Effect.gen(function* () {
      const snoozed = { ...finished, snoozedAt: at(-MINUTE_MS) };
      expect(yield* shows({ ...snoozed, snoozedUntil: at(MINUTE_MS) })).toBe(false);
      expect(yield* shows({ ...snoozed, snoozedUntil: at(-1) })).toBe(true);
    }),
  );

  it.effect("a live child's request shows on the thread, as the relay walks up to it", () =>
    Effect.gen(function* () {
      expect(yield* shows(finished, [child("kid", threadId)])).toBe(false);
      // A grandchild, through a child that waits on nothing itself.
      const kid = child("kid", threadId, false);
      expect(yield* shows(finished, [kid, child("grandkid", "kid")])).toBe(false);
      // The grandchild's parent is missing from the index: unsure, so not shown.
      const grandkid = child("grandkid", "kid");
      expect(yield* shows(finished, [grandkid])).toBe(false);
    }),
  );

  it.effect("children that show nothing here leave the finish shown", () =>
    Effect.gen(function* () {
      // Another thread's child, a child with no request, one that cannot be live.
      const other = { ...finished, id: ThreadId.make("other-thread") };
      const otherKid = child("other-kid", "other-thread");
      expect(yield* shows(finished, [otherKid], [entry(other), entry(otherKid)])).toBe(true);
      expect(yield* shows(finished, [child("kid", threadId, false)])).toBe(true);
      const kid = child("kid", threadId);
      expect(yield* shows(finished, [kid], [entry(kid, false)])).toBe(true);
      // In the index but already gone when read.
      expect(yield* shows(finished, [], [entry(kid)])).toBe(true);
      // A parent loop that never reaches the thread ends the walk.
      const a = child("a", "b", false);
      const b = child("b", "a");
      expect(yield* shows(finished, [a, b])).toBe(true);
    }),
  );
});
