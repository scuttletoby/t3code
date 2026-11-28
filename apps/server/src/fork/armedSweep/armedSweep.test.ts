/**
 * Fork (issue 880): the armed PR sweep against a fake world: once per head and reason, joined,
 * handed over; run twice, stopped half-way, old/newer/bad/empty state; dry sends nothing; one
 * GitHub request per sweep.
 */
import { describe, expect, it } from "@effect/vitest";
import {
  type ArmedSweepSettings,
  DEFAULT_ARMED_SWEEP_SETTINGS,
  ProjectId,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";

import {
  type ArmedLink,
  type ArmedThread,
  type ArmedWake,
  ArmedWorldError,
  type ArmedWorld,
  makeSweep,
} from "./armedSweep.ts";
import { readState, STATE_VERSION } from "./armedState.ts";

const MIN = 60_000;
const T0 = 10_000 * MIN;
const HEAD = "a".repeat(40);
const PROJECT = ProjectId.make("project-1");
const OWNER = ThreadId.make("thread-owner");
const ORCHESTRATOR = ThreadId.make("thread-orchestrator");
const CHILD = ThreadId.make("thread-child");

const on: ArmedSweepSettings = { ...DEFAULT_ARMED_SWEEP_SETTINGS, mode: "on" };

interface FakePull {
  readonly number: number;
  readonly state?: "OPEN" | "CLOSED";
  readonly merged?: boolean;
  readonly armed?: boolean;
  readonly head?: string;
  readonly mergeable?: "MERGEABLE" | "CONFLICTING" | "UNKNOWN";
  /** Check runs: conclusion, or null while running. */
  readonly checks?: ReadonlyArray<{
    name: string;
    conclusion: string | null;
    completedAt?: number;
  }>;
}

const nodeOf = (pull: FakePull) => ({
  id: `PR_${pull.number}`,
  number: pull.number,
  url: `https://github.com/acme/app/pull/${pull.number}`,
  title: `Change ${pull.number}`,
  state: pull.state ?? "OPEN",
  merged: pull.merged ?? false,
  headRefOid: pull.head ?? HEAD,
  mergeable: pull.mergeable ?? "MERGEABLE",
  repository: { nameWithOwner: "acme/app" },
  autoMergeRequest: pull.armed === false ? null : { enabledAt: "2026-11-27T00:00:00Z" },
  commits: {
    nodes: [
      {
        commit: {
          statusCheckRollup: {
            contexts: {
              nodes: (pull.checks ?? [{ name: "test", conclusion: "SUCCESS" }]).map((check) => ({
                __typename: "CheckRun",
                name: check.name,
                status: check.conclusion === null ? "IN_PROGRESS" : "COMPLETED",
                conclusion: check.conclusion,
                completedAt:
                  check.conclusion === null
                    ? null
                    : DateTime.formatIso(DateTime.makeUnsafe(check.completedAt ?? T0 - 60 * MIN)),
              })),
            },
          },
        },
      },
    ],
  },
});

const red = (number: number, extra: Partial<FakePull> = {}): FakePull => ({
  number,
  checks: [{ name: "test", conclusion: "FAILURE" }],
  ...extra,
});

interface World {
  readonly world: ArmedWorld;
  /** Open pull requests the search returns. */
  open: ReadonlyArray<FakePull>;
  /** What `nodes(ids:)` returns for a followed id that is no longer open. */
  byId: Record<string, FakePull>;
  links: ReadonlyArray<ArmedLink>;
  threads: Record<string, ArmedThread>;
  orchestrator: ThreadId | null;
  state: string | null;
  readonly sent: Array<ArmedWake>;
  readonly requests: Array<string>;
  readonly logs: Array<string>;
  readonly accepted: Set<string>;
  sendResult: "sent" | "retry" | "fail" | "die";
  graphqlFails: boolean;
  writes: number;
}

const makeWorld = (): World => {
  const w: World = {
    open: [],
    byId: {},
    links: [{ repository: "acme/app", number: 1, threadId: OWNER, projectId: PROJECT }],
    threads: {
      [OWNER]: { threadId: OWNER, projectId: PROJECT, parentThreadId: null, gone: false },
      [ORCHESTRATOR]: {
        threadId: ORCHESTRATOR,
        projectId: PROJECT,
        parentThreadId: null,
        gone: false,
      },
      [CHILD]: { threadId: CHILD, projectId: PROJECT, parentThreadId: OWNER, gone: false },
    },
    orchestrator: ORCHESTRATOR,
    state: null,
    sent: [],
    requests: [],
    logs: [],
    accepted: new Set(),
    sendResult: "sent",
    graphqlFails: false,
    writes: 0,
    world: undefined as never,
  };
  (w as { world: ArmedWorld }).world = {
    sources: () =>
      Effect.sync(() => ({
        repositories: [{ repository: "acme/app", projectId: PROJECT }],
        links: w.links,
      })),
    graphql: (document) =>
      Effect.suspend(() => {
        w.requests.push(document);
        if (w.graphqlFails) return Effect.fail(new ArmedWorldError({ message: "rate limited" }));
        const ids = /nodes\(ids: (\[[^\]]*\])\)/.exec(document);
        const followed = ids === null ? undefined : (JSON.parse(ids[1]!) as string[]);
        return Effect.succeed({
          data: {
            s0: { pageInfo: { hasNextPage: false, endCursor: null }, nodes: w.open.map(nodeOf) },
            ...(followed === undefined
              ? {}
              : {
                  followed: followed.map((id) => {
                    const open = w.open.find((pull) => `PR_${pull.number}` === id);
                    const pull = open ?? w.byId[id];
                    return pull === undefined ? null : nodeOf(pull);
                  }),
                }),
          },
        });
      }),
    thread: (threadId) => Effect.sync(() => w.threads[threadId] ?? null),
    fallback: () => Effect.sync(() => w.orchestrator),
    send: (wake) =>
      Effect.suspend(() => {
        if (w.sendResult === "die") return Effect.die("server stopped");
        if (w.sendResult === "fail")
          return Effect.fail(new ArmedWorldError({ message: "send failed" }));
        if (w.sendResult === "sent") {
          w.sent.push(wake);
          w.accepted.add(wake.commandId);
        }
        return Effect.succeed(w.sendResult);
      }),
    accepted: (commandId) => Effect.sync(() => w.accepted.has(commandId)),
    readState: Effect.sync(() => w.state),
    writeState: (text) =>
      Effect.sync(() => {
        w.writes++;
        w.state = text;
      }),
    log: (message) => Effect.sync(() => void w.logs.push(message)),
  };
  return w;
};

const sweepAt = (w: World, nowMs: number, settings: ArmedSweepSettings = on) =>
  makeSweep(w.world).sweep(settings, nowMs);

const stateOf = (w: World) => {
  const read = readState(w.state);
  if (read.type !== "ok") throw new Error(`state not ok: ${read.type}`);
  return read.state;
};

describe("ArmedSweep", () => {
  it.effect("off: reads and sends nothing", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.open = [red(1)];
      expect(yield* sweepAt(w, T0, DEFAULT_ARMED_SWEEP_SETTINGS)).toBeNull();
      expect(w.requests).toHaveLength(0);
      expect(w.writes).toBe(0);
    }),
  );

  it.effect("a red linked pull request wakes its thread once; a second sweep sends nothing", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.open = [red(1)];
      const first = yield* sweepAt(w, T0);
      expect(w.sent).toHaveLength(1);
      expect(w.sent[0]!.threadId).toBe(OWNER);
      expect(w.sent[0]!.text).toContain("acme/app#1");
      expect(w.sent[0]!.text).toContain("red, failing: test");
      expect(first?.pullRequests).toMatchObject([{ state: "red", wake: "sent", threadId: OWNER }]);
      const second = yield* sweepAt(w, T0 + 5 * MIN);
      expect(w.sent).toHaveLength(1);
      expect(second?.pullRequests).toMatchObject([{ state: "red", wake: "sent" }]);
      expect(stateOf(w).pending).toEqual([]);
    }),
  );

  it.effect("asks GitHub once per sweep, the followed pull requests in the same request", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.open = [red(1)];
      yield* sweepAt(w, T0);
      expect(w.requests).toHaveLength(1);
      expect(w.requests[0]).not.toContain("followed:");
      yield* sweepAt(w, T0 + 5 * MIN);
      expect(w.requests).toHaveLength(2);
      expect(w.requests[1]).toContain('followed: nodes(ids: ["PR_1"])');
    }),
  );

  it.effect("dry: lists and logs, sends nothing, marks nothing sent; on then sends it", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.open = [red(1)];
      const dry = yield* sweepAt(w, T0, { ...on, mode: "dry" });
      expect(w.sent).toHaveLength(0);
      expect(dry?.pullRequests).toMatchObject([{ state: "red", wake: "would-send" }]);
      expect(w.logs.some((line) => line.includes("would wake"))).toBe(true);
      expect(stateOf(w).sent).toEqual({});
      expect(Object.keys(stateOf(w).tracked)).toEqual(["PR_1"]);
      yield* sweepAt(w, T0 + 5 * MIN, { ...on, mode: "dry" });
      expect(w.sent).toHaveLength(0);
      yield* sweepAt(w, T0 + 10 * MIN);
      expect(w.sent).toHaveLength(1);
    }),
  );

  it.effect("joins every wake of one thread into one message", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.links = [
        { repository: "acme/app", number: 1, threadId: OWNER, projectId: PROJECT },
        { repository: "acme/app", number: 2, threadId: OWNER, projectId: PROJECT },
      ];
      w.open = [red(1), { number: 2, mergeable: "CONFLICTING" }];
      yield* sweepAt(w, T0);
      expect(w.sent).toHaveLength(1);
      expect(w.sent[0]!.text).toContain("acme/app#1");
      expect(w.sent[0]!.text).toContain("acme/app#2");
      expect(w.sent[0]!.summary).toBe("2 armed pull requests cannot merge by themselves");
    }),
  );

  it.effect("a delegated child's link wakes the top-level thread above it", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.links = [{ repository: "acme/app", number: 1, threadId: CHILD, projectId: PROJECT }];
      w.open = [red(1)];
      yield* sweepAt(w, T0);
      expect(w.sent.map((wake) => wake.threadId)).toEqual([OWNER]);
    }),
  );

  it.effect("an archived owner hands over to the project's orchestrator", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.threads = { ...w.threads, [OWNER]: { ...w.threads[OWNER]!, gone: true } };
      w.open = [red(1)];
      yield* sweepAt(w, T0);
      expect(w.sent.map((wake) => wake.threadId)).toEqual([ORCHESTRATOR]);
      expect(w.sent[0]!.text).toContain("its thread is archived");
    }),
  );

  it.effect(
    "unlinked armed pull requests: ignored by scope linked, the orchestrator's with all",
    () =>
      Effect.gen(function* () {
        const w = makeWorld();
        w.links = [];
        w.open = [red(5)];
        const linked = yield* sweepAt(w, T0);
        expect(w.sent).toHaveLength(0);
        expect(linked?.pullRequests).toEqual([]);
        expect(stateOf(w).tracked).toEqual({});
        yield* sweepAt(w, T0 + 5 * MIN, { ...on, scope: "all" });
        expect(w.sent.map((wake) => wake.threadId)).toEqual([ORCHESTRATOR]);
        expect(w.sent[0]!.text).toContain("no thread links it");
      }),
  );

  it.effect("no thread to send to: listed as no-thread, sent once one is set", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.links = [];
      w.orchestrator = null;
      w.open = [red(5)];
      const first = yield* sweepAt(w, T0, { ...on, scope: "all" });
      expect(first?.pullRequests).toMatchObject([{ wake: "no-thread", threadId: null }]);
      w.orchestrator = ORCHESTRATOR;
      yield* sweepAt(w, T0 + 5 * MIN, { ...on, scope: "all" });
      expect(w.sent).toHaveLength(1);
    }),
  );

  it.effect("caps the wakes of one head; a new head wakes again", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      const settings = { ...on, maxWakesPerHead: 1 };
      w.open = [{ number: 1, checks: [{ name: "test", conclusion: null }] }];
      yield* sweepAt(w, T0, settings);
      yield* sweepAt(w, T0 + 30 * MIN, settings);
      expect(w.sent.map((wake) => wake.text.includes("stalled"))).toEqual([true]);
      w.open = [red(1)];
      const capped = yield* sweepAt(w, T0 + 60 * MIN, settings);
      expect(w.sent).toHaveLength(1);
      expect(capped?.pullRequests).toMatchObject([{ state: "red", wake: "capped" }]);
      w.open = [red(1, { head: "b".repeat(40) })];
      yield* sweepAt(w, T0 + 90 * MIN, settings);
      expect(w.sent).toHaveLength(2);
    }),
  );

  it.effect("closed while followed: one closed wake, then forgotten", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.open = [{ number: 1 }];
      yield* sweepAt(w, T0);
      expect(w.sent).toHaveLength(0);
      w.open = [];
      w.byId = { PR_1: { number: 1, state: "CLOSED" } };
      yield* sweepAt(w, T0 + 5 * MIN);
      expect(w.sent).toHaveLength(1);
      expect(w.sent[0]!.text).toContain("closed, closed without merging");
      expect(stateOf(w).tracked).toEqual({});
      yield* sweepAt(w, T0 + 10 * MIN);
      expect(w.sent).toHaveLength(1);
    }),
  );

  it.effect("merged while followed: forgotten without a word", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.open = [{ number: 1 }];
      yield* sweepAt(w, T0);
      w.open = [];
      w.byId = { PR_1: { number: 1, state: "CLOSED", merged: true } };
      yield* sweepAt(w, T0 + 5 * MIN);
      expect(w.sent).toHaveLength(0);
      expect(stateOf(w).tracked).toEqual({});
    }),
  );

  it.effect("a followed pull request GitHub no longer returns is kept, nothing said", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.open = [{ number: 1 }];
      yield* sweepAt(w, T0);
      w.open = [];
      yield* sweepAt(w, T0 + 5 * MIN);
      expect(w.sent).toHaveLength(0);
      expect(Object.keys(stateOf(w).tracked)).toEqual(["PR_1"]);
    }),
  );

  it.effect("GitHub refuses: the sweep fails, the state file is untouched, nothing is sent", () =>
    Effect.gen(function* () {
      const w = makeWorld();
      w.open = [red(1)];
      w.graphqlFails = true;
      const exit = yield* Effect.exit(sweepAt(w, T0));
      expect(exit._tag).toBe("Failure");
      expect(w.writes).toBe(0);
      expect(w.sent).toHaveLength(0);
    }),
  );

  describe("stops half-way", () => {
    it.effect("stopped right before the send: the next sweep sends it, with the same command", () =>
      Effect.gen(function* () {
        const w = makeWorld();
        w.open = [red(1)];
        w.sendResult = "die";
        expect((yield* Effect.exit(sweepAt(w, T0)))._tag).toBe("Failure");
        const pending = stateOf(w).pending;
        expect(pending).toHaveLength(1);
        w.sendResult = "sent";
        yield* sweepAt(w, T0 + 5 * MIN);
        expect(w.sent.map((wake) => wake.commandId)).toEqual([pending[0]!.commandId]);
        expect(stateOf(w).pending).toEqual([]);
      }),
    );

    it.effect("stopped right after the send: the receipt settles it, nothing is sent twice", () =>
      Effect.gen(function* () {
        const w = makeWorld();
        w.open = [red(1)];
        yield* sweepAt(w, T0);
        expect(w.sent).toHaveLength(1);
        // The state as it was written before the send: pending, not yet sent.
        const before = stateOf(w);
        const key = Object.keys(before.sent)[0]!;
        w.state = JSON.stringify({
          version: STATE_VERSION,
          tracked: before.tracked,
          sent: {},
          pending: [{ commandId: w.sent[0]!.commandId, keys: [key] }],
        });
        yield* sweepAt(w, T0 + 5 * MIN);
        expect(w.sent).toHaveLength(1);
        expect(Object.keys(stateOf(w).sent)).toEqual([key]);
      }),
    );

    it.effect("a send that fails stays pending and is not marked sent", () =>
      Effect.gen(function* () {
        const w = makeWorld();
        w.open = [red(1)];
        w.sendResult = "fail";
        const report = yield* sweepAt(w, T0);
        expect(report?.pullRequests).toMatchObject([{ wake: "retry" }]);
        expect(stateOf(w).pending).toHaveLength(1);
        expect(stateOf(w).sent).toEqual({});
        w.sendResult = "sent";
        yield* sweepAt(w, T0 + 5 * MIN);
        expect(w.sent).toHaveLength(1);
      }),
    );

    it.effect("no agent slot: retried next sweep", () =>
      Effect.gen(function* () {
        const w = makeWorld();
        w.open = [red(1)];
        w.sendResult = "retry";
        yield* sweepAt(w, T0);
        expect(stateOf(w)).toMatchObject({ sent: {}, pending: [] });
        w.sendResult = "sent";
        yield* sweepAt(w, T0 + 5 * MIN);
        expect(w.sent).toHaveLength(1);
      }),
    );
  });

  describe("state file", () => {
    it.effect("empty (first run): a red already there wakes", () =>
      Effect.gen(function* () {
        const w = makeWorld();
        w.open = [red(1)];
        yield* sweepAt(w, T0);
        expect(w.sent).toHaveLength(1);
        expect(w.logs).toEqual([]);
      }),
    );

    it.effect("not JSON, no version or an old version: starts empty and says so", () =>
      Effect.gen(function* () {
        for (const text of ["{oops", '{"tracked":{}}', '{"version":0,"sent":{}}']) {
          const w = makeWorld();
          w.state = text;
          w.open = [red(1)];
          yield* sweepAt(w, T0);
          expect(w.sent).toHaveLength(1);
          expect(w.logs).toHaveLength(1);
          expect(stateOf(w).tracked).toHaveProperty("PR_1");
        }
      }),
    );

    it.effect("from a newer build: stands still, never overwritten", () =>
      Effect.gen(function* () {
        const w = makeWorld();
        w.state = JSON.stringify({ version: STATE_VERSION + 1 });
        w.open = [red(1)];
        const report = yield* sweepAt(w, T0);
        expect(report?.error).toContain("newer");
        expect(w.requests).toHaveLength(0);
        expect(w.writes).toBe(0);
      }),
    );

    it.effect("a bad entry is dropped alone", () =>
      Effect.gen(function* () {
        const w = makeWorld();
        w.open = [red(1)];
        yield* sweepAt(w, T0);
        const good = stateOf(w);
        w.state = JSON.stringify({
          version: STATE_VERSION,
          tracked: { ...good.tracked, PR_9: { id: "PR_9" } },
          sent: { ...good.sent, bad: "yesterday" },
          pending: [{ commandId: 4 }],
        });
        yield* sweepAt(w, T0 + 5 * MIN);
        expect(w.sent).toHaveLength(1);
        expect(Object.keys(stateOf(w).tracked)).toEqual(["PR_1"]);
      }),
    );
  });
});
