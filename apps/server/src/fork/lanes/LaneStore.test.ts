/** Fork (issue 1156, phase 2): the lane store over a real folder, one store per server start. */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import {
  DEFAULT_LANE_DEFINITIONS,
  type LaneDefinition,
  ProjectId,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";

import type { ServerSettings } from "@t3tools/contracts";
import { ServerSettingsService } from "../../serverSettings.ts";
import * as LaneStore from "./LaneStore.ts";
import { NOTES_PER_LANE } from "./laneState.ts";

const PROJECT = ProjectId.make("project-1");
const OTHER_PROJECT = ProjectId.make("project-2");
const A = ThreadId.make("lane-thread-a");
const B = ThreadId.make("lane-thread-b");
const C = ThreadId.make("lane-thread-c");
const ISSUES = ThreadId.make("issues-thread");
const PR = "pr:owner/repo#12";

/** A second lane that also owns pull requests, so two lanes can want one. */
const CI: LaneDefinition = { description: "Keeps CI green.", owns: ["pr"], driver: "agent" };

interface Lanes {
  enabled: boolean;
  definitions: { [name: string]: LaneDefinition };
}

const harness = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const directory = path.join(yield* fs.makeTempDirectoryScoped({ prefix: "lanes-" }), "lanes");
  const lanes: Lanes = { enabled: true, definitions: { ...DEFAULT_LANE_DEFINITIONS, ci: CI } };
  const settings = Layer.mock(ServerSettingsService)({
    getSettings: Effect.sync(() => ({ lanes: { ...lanes } }) as unknown as ServerSettings),
  });
  /** One server start: a new store over the same folder. */
  const start = LaneStore.make({ directory }).pipe(Effect.provide(settings));
  const file = path.join(directory, `${PROJECT}.json`);
  const readFile = fs.readFileString(file);
  return { fs, lanes, start, file, readFile, directory };
});

const reasonOf = <A>(exit: Exit.Exit<A, LaneStore.LaneStoreRefused>) =>
  Exit.isFailure(exit) && exit.cause.reasons[0]?._tag === "Fail"
    ? (exit.cause.reasons[0].error as LaneStore.LaneStoreRefused).reason
    : Exit.isSuccess(exit)
      ? "ok"
      : "defect";

const actor = (lane: string, generation: number, threadId: ThreadId) => ({
  lane,
  generation,
  threadId,
});

it.layer(NodeServices.layer)("LaneStore", (it) => {
  it.effect("keeps the registry and claims across a restart", () =>
    Effect.gen(function* () {
      const { start, readFile } = yield* harness;
      const first = yield* start;
      const entry = yield* first.startLane(PROJECT, "pr-queue", A);
      assert.strictEqual(entry.generation, 1);
      const { claim } = yield* first.claim(PROJECT, actor("pr-queue", 1, A), PR);
      yield* first.note(PROJECT, actor("pr-queue", 1, A), "Reviewing #12.", [PR]);
      yield* first.escalate(PROJECT, actor("pr-queue", 1, A), "esc-1", "Merge #12?", [PR]);

      const again = yield* start;
      assert.deepStrictEqual(yield* again.ownerOf(PROJECT, PR), claim);
      const board = yield* again.board(PROJECT);
      assert.strictEqual(board.lanes["pr-queue"]?.status, "running");
      assert.deepStrictEqual(
        board.outbox.map((entry) => entry.id),
        ["esc-1"],
      );
      assert.deepStrictEqual(
        board.notes.map((note) => note.text),
        ["Reviewing #12."],
      );
      // The lane's thread still acts for it after the restart; another lane is still refused.
      yield* again.startLane(PROJECT, "ci", C);
      const exit = yield* Effect.exit(again.claim(PROJECT, actor("ci", 1, C), PR));
      assert.strictEqual(reasonOf(exit), "claimed");
      // A repeated start of the same thread changes nothing; another thread is refused.
      const before = yield* readFile;
      yield* again.startLane(PROJECT, "pr-queue", A);
      assert.strictEqual(yield* readFile, before);
      assert.strictEqual(
        reasonOf(yield* Effect.exit(again.startLane(PROJECT, "pr-queue", B))),
        "already-running",
      );
      // Other projects keep their own file.
      assert.deepStrictEqual((yield* again.board(OTHER_PROJECT)).claims, []);
    }),
  );

  it.effect("lets exactly one of two claims on one item win", () =>
    Effect.gen(function* () {
      const { start } = yield* harness;
      const store = yield* start;
      yield* store.startLane(PROJECT, "pr-queue", A);
      yield* store.startLane(PROJECT, "ci", C);
      const exits = yield* Effect.all(
        [
          Effect.exit(store.claim(PROJECT, actor("pr-queue", 1, A), PR)),
          Effect.exit(store.claim(PROJECT, actor("ci", 1, C), PR)),
        ],
        { concurrency: "unbounded" },
      );
      assert.deepStrictEqual(exits.map(reasonOf).toSorted(), ["claimed", "ok"]);
      const owner = yield* store.ownerOf(PROJECT, PR);
      const winner = exits.findIndex(Exit.isSuccess) === 0 ? "pr-queue" : "ci";
      assert.strictEqual(owner?.lane, winner);
      // The holder's own repeat is kept as it was; the loser cannot release it.
      const loser = winner === "ci" ? actor("pr-queue", 1, A) : actor("ci", 1, C);
      assert.strictEqual(
        reasonOf(yield* Effect.exit(store.release(PROJECT, loser, PR))),
        "not-holder",
      );
      // A kind the lane does not own, and text that is not an item, are refused.
      assert.strictEqual(
        reasonOf(yield* Effect.exit(store.claim(PROJECT, actor("ci", 1, C), "issue:owner/repo#3"))),
        "kind-not-owned",
      );
      assert.strictEqual(
        reasonOf(yield* Effect.exit(store.claim(PROJECT, actor("ci", 1, C), "pr:12"))),
        "bad-item",
      );
    }),
  );

  it.effect("moves a dead lane thread's claims to its replacement and refuses the old thread", () =>
    Effect.gen(function* () {
      const { start, lanes } = yield* harness;
      const store = yield* start;
      yield* store.startLane(PROJECT, "pr-queue", A);
      yield* store.claim(PROJECT, actor("pr-queue", 1, A), PR);
      // Thread A dies; the lane stays running and its claim still refuses other lanes.
      yield* store.startLane(PROJECT, "ci", C);
      assert.strictEqual(
        reasonOf(yield* Effect.exit(store.claim(PROJECT, actor("ci", 1, C), PR))),
        "claimed",
      );
      const replaced = yield* store.replaceLaneThread(PROJECT, "pr-queue", 1, B);
      assert.strictEqual(replaced.generation, 2);
      const owner = yield* store.ownerOf(PROJECT, PR);
      assert.deepStrictEqual([owner?.lane, owner?.generation, owner?.threadId], ["pr-queue", 2, B]);
      // The old thread can no longer act for the lane.
      for (const step of [
        store.claim(PROJECT, actor("pr-queue", 1, A), "pr:owner/repo#13"),
        store.release(PROJECT, actor("pr-queue", 1, A), PR),
        store.note(PROJECT, actor("pr-queue", 1, A), "still here", []),
        store.escalate(PROJECT, actor("pr-queue", 1, A), "esc-a", "still here", []),
      ] as ReadonlyArray<Effect.Effect<unknown, LaneStore.LaneStoreRefused>>) {
        assert.strictEqual(reasonOf(yield* Effect.exit(step)), "stale-owner");
      }
      // Repeating the replacement (a step run twice) changes nothing; replacing an old one fails.
      assert.strictEqual((yield* store.replaceLaneThread(PROJECT, "pr-queue", 1, B)).generation, 2);
      assert.strictEqual(
        reasonOf(yield* Effect.exit(store.replaceLaneThread(PROJECT, "pr-queue", 1, C))),
        "stale-owner",
      );
      // A lane removed from the settings leaves a stale claim: the next claim takes it.
      delete lanes.definitions["pr-queue"];
      assert.isNull(yield* store.ownerOf(PROJECT, PR));
      const board = yield* store.board(PROJECT);
      assert.deepStrictEqual(
        board.claims.map((entry) => [entry.claim.item, entry.live]),
        [[PR, false]],
      );
      const taken = yield* store.claim(PROJECT, actor("ci", 1, C), PR);
      assert.strictEqual(taken.takenFrom?.lane, "pr-queue");
      assert.strictEqual(taken.claim.lane, "ci");
      // Stopping a lane releases its claims, and a new start is a new generation.
      const released = yield* store.stopLane(PROJECT, "ci");
      assert.deepStrictEqual(
        released.map((claim) => claim.item),
        [PR],
      );
      assert.strictEqual((yield* store.startLane(PROJECT, "ci", ISSUES)).generation, 2);
      assert.strictEqual(
        reasonOf(yield* Effect.exit(store.claim(PROJECT, actor("ci", 1, C), PR))),
        "stale-owner",
      );
    }),
  );

  it.effect("refuses every step while lanes are off and keeps the claims on disk", () =>
    Effect.gen(function* () {
      const { start, lanes, readFile } = yield* harness;
      const store = yield* start;
      yield* store.startLane(PROJECT, "pr-queue", A);
      const { claim } = yield* store.claim(PROJECT, actor("pr-queue", 1, A), PR);
      const before = yield* readFile;
      lanes.enabled = false;
      const off = yield* start;
      for (const step of [
        off.claim(PROJECT, actor("pr-queue", 1, A), "pr:owner/repo#13"),
        off.release(PROJECT, actor("pr-queue", 1, A), PR),
        off.stopLane(PROJECT, "pr-queue"),
        off.startLane(PROJECT, "issues", ISSUES),
        off.ownerOf(PROJECT, PR),
        off.board(PROJECT),
        off.ackEscalations(PROJECT, ["x"]),
      ]) {
        assert.strictEqual(
          reasonOf(yield* Effect.exit(step as Effect.Effect<unknown, LaneStore.LaneStoreRefused>)),
          "lanes-off",
        );
      }
      assert.isNull(yield* off.laneOfRun(PROJECT, A, null));
      assert.strictEqual(yield* readFile, before);
      // On again: the claim and its lane are as they were.
      lanes.enabled = true;
      const on = yield* start;
      assert.deepStrictEqual(yield* on.ownerOf(PROJECT, PR), claim);
      assert.strictEqual(yield* on.laneOfRun(PROJECT, ThreadId.make("worker"), A), "pr-queue");
    }),
  );

  it.effect("treats a lane file it cannot read as unknown, not empty, and keeps it", () =>
    Effect.gen(function* () {
      const { start, fs, file, readFile, directory } = yield* harness;
      const store = yield* start;
      yield* fs.makeDirectory(directory, { recursive: true });
      for (const text of [
        "{not json",
        JSON.stringify({ v: 2, projectId: PROJECT, lanes: {}, claims: {} }),
        JSON.stringify({
          v: 1,
          projectId: OTHER_PROJECT,
          lanes: {},
          claims: {},
          outbox: [],
          notes: [],
        }),
      ]) {
        yield* fs.writeFileString(file, text);
        for (const step of [store.startLane(PROJECT, "pr-queue", A), store.ownerOf(PROJECT, PR)]) {
          assert.strictEqual(
            reasonOf(
              yield* Effect.exit(step as Effect.Effect<unknown, LaneStore.LaneStoreRefused>),
            ),
            "store-unreadable",
          );
        }
        assert.strictEqual(yield* readFile, text);
        assert.isNull(yield* store.laneOfRun(PROJECT, A, null));
      }
      // Readable again: steps work.
      yield* fs.remove(file);
      assert.strictEqual((yield* store.startLane(PROJECT, "pr-queue", A)).generation, 1);
    }),
  );

  it.effect("delivers escalations once per id, keeps the last notes, and refuses bad text", () =>
    Effect.gen(function* () {
      const { start, readFile } = yield* harness;
      const store = yield* start;
      yield* store.startLane(PROJECT, "pr-queue", A);
      const me = actor("pr-queue", 1, A);
      yield* store.escalate(PROJECT, me, "esc-1", "Merge #12?", [PR]);
      yield* store.escalate(PROJECT, me, "esc-1", "Merge #12? (again)", [PR]);
      yield* store.escalate(PROJECT, me, "esc-2", "Close #9?", []);
      assert.deepStrictEqual(
        (yield* store.board(PROJECT)).outbox.map((entry) => [entry.id, entry.text]),
        [
          ["esc-1", "Merge #12?"],
          ["esc-2", "Close #9?"],
        ],
      );
      assert.strictEqual(yield* store.ackEscalations(PROJECT, ["esc-1", "gone"]), 1);
      assert.strictEqual(yield* store.ackEscalations(PROJECT, ["esc-1"]), 0);
      for (let index = 0; index <= NOTES_PER_LANE; index += 1) {
        yield* store.note(PROJECT, me, `note ${index}`, []);
      }
      const notes = (yield* store.board(PROJECT)).notes;
      assert.strictEqual(notes.length, NOTES_PER_LANE);
      assert.strictEqual(notes[0]?.text, "note 1");
      // Text the file could not read back is refused and never written.
      const before = yield* readFile;
      for (const step of [
        store.note(PROJECT, me, "   ", []),
        store.note(PROJECT, me, "x".repeat(2001), []),
        store.note(PROJECT, me, "fine", ["pr:12"]),
        store.escalate(PROJECT, me, " ", "Merge?", []),
      ]) {
        assert.strictEqual(
          reasonOf(yield* Effect.exit(step as Effect.Effect<unknown, LaneStore.LaneStoreRefused>)),
          "bad-input",
        );
      }
      assert.strictEqual(yield* readFile, before);
      assert.strictEqual(yield* store.laneOfRun(PROJECT, A, null), "pr-queue");
      assert.isNull(yield* store.laneOfRun(PROJECT, ThreadId.make("main"), null));
    }),
  );
});
