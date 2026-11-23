/**
 * Fork (review rules): a wait with `action: "lens_start"` (lens_start {afterCi: true}). Its action
 * runs once the wake is in the thread and only when the narrowed checks passed at the head asked
 * for; red, a moved head and a timeout start nothing. A restart half-way runs it again (it is
 * idempotent), never loses it.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitRecord } from "@t3tools/contracts/waitFor";
import type { Judgement } from "@t3tools/shared/waitFor";
import * as Deferred from "effect/Deferred";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import { TestClock } from "effect/testing";

import { ServerSettingsService } from "../serverSettings.ts";
import type { WaitDelivery, WaitDeliveryResult } from "./WaitDelivery.ts";
import type { WaitJudge } from "./WaitJudges.ts";
import * as WaitService from "./WaitService.ts";
import { WAIT_STORE_FILE } from "./WaitStore.ts";

const NOW = Date.parse("2026-11-22T12:00:00.000Z");
const THREAD = ThreadId.make("thread-orchestrator");
const PROJECT = ProjectId.make("project-a");
const HEAD = "a".repeat(40);
const ON = { waitFor: { enabled: true } } as const;

const storePath = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-action-" });
  return path.join(root, WAIT_STORE_FILE);
});

/** A pr judge that answers what the test says, and records the targets it was asked. */
const scriptedPrJudge = () => {
  let next: Judgement = { type: "notYet" };
  const asked: Array<unknown> = [];
  const judge: WaitJudge<"pr"> = (target) =>
    Effect.sync(() => {
      asked.push(target.spec);
      return next;
    });
  return { judge, asked, answer: (judgement: Judgement) => void (next = judgement) };
};

/** A delivery that keeps each message once (by id), like the real one; `lose` drops the receipt. */
const recordingDelivery = () => {
  const sent: Array<WaitService.WaitMessage> = [];
  const inThread = new Set<string>();
  let lose = false;
  /** Message ids refused once (nothing recorded), then let through. */
  const refuseOnce = new Set<string>();
  const delivery: WaitDelivery = {
    receiver: () => Effect.succeed({ type: "ok" }),
    deliver: (message) =>
      Effect.sync((): WaitDeliveryResult => {
        if (inThread.has(message.messageId)) return { type: "delivered" };
        const once = [...refuseOnce].find((part) => message.messageId.includes(part));
        if (once !== undefined) {
          refuseOnce.delete(once);
          return { type: "retry", detail: "busy" };
        }
        inThread.add(message.messageId);
        sent.push(message);
        // Accepted, but the answer is lost (a crash right after the send).
        return lose ? { type: "retry", detail: "lost answer" } : { type: "delivered" };
      }),
  };
  return {
    delivery,
    sent,
    losing: (value: boolean) => void (lose = value),
    refuseOnce: (part: string) => void refuseOnce.add(part),
  };
};

/** The action, as lens_start installs it: records each run, answers what the test says. */
const recordingAction = () => {
  const runs: Array<WaitRecord> = [];
  let ready = true;
  let answer: string | null = null;
  let hang = false;
  /** Done once the action has run (a milestone the timeout test awaits). */
  const ran = Deferred.makeUnsafe<void>();
  const action: WaitService.WaitAction = {
    ready: Effect.sync(() => ready),
    run: (wait) =>
      Effect.suspend(() => {
        runs.push(wait);
        Deferred.doneUnsafe(ran, Exit.void);
        return hang ? Effect.never : Effect.succeed(answer);
      }),
  };
  return {
    action,
    runs,
    ran,
    setReady: (value: boolean) => void (ready = value),
    setAnswer: (value: string | null) => void (answer = value),
    setHang: (value: boolean) => void (hang = value),
  };
};

const make = (
  filePath: string,
  judge: WaitJudge<"pr">,
  delivery: WaitDelivery,
  action?: WaitService.WaitAction,
) =>
  WaitService.make({
    filePath,
    delivery,
    fileChildJudge: null,
    judges: { pr: judge },
    ...(action === undefined ? {} : { action }),
  });

const registerAfterCi = (service: WaitService.WaitService) =>
  service.register({
    threadId: THREAD,
    projectId: PROJECT,
    input: { kind: "pr", pr: { number: 7, repository: "o/r" }, timeoutMinutes: 60 },
    narrow: { checks: ["ci-result"], head: HEAD },
    action: "lens_start",
  });

const passed: Judgement = { type: "met", outcome: "passed", detail: "ci-result passed." };

it.layer(NodeServices.layer)("WaitService action: lens_start", (it) => {
  it.effect("green at the same head: the wake is sent, then the action runs once", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const filePath = yield* storePath;
      const pr = scriptedPrJudge();
      const send = recordingDelivery();
      const action = recordingAction();
      const service = yield* make(filePath, pr.judge, send.delivery, action.action);

      const registered = yield* registerAfterCi(service);
      assert.strictEqual(registered.state, "waiting");
      // The judge reads the narrowed target.
      assert.deepStrictEqual(pr.asked[0], {
        number: 7,
        repository: "o/r",
        until: "checks",
        checks: ["ci-result"],
        head: HEAD,
      });
      assert.strictEqual(action.runs.length, 0);

      pr.answer(passed);
      yield* TestClock.adjust(130_000);
      yield* service.runDue();
      assert.strictEqual(send.sent.length, 1);
      assert.include(send.sent[0]!.text, "The server starts the review lenses");
      assert.strictEqual(action.runs.length, 1);
      assert.strictEqual(action.runs[0]!.action, "lens_start");

      // Running again: nothing more is sent or started.
      yield* TestClock.adjust(130_000);
      yield* service.runDue();
      yield* service.deliverDue();
      assert.strictEqual(send.sent.length, 1);
      assert.strictEqual(action.runs.length, 1);
      // The same call again while it is delivered: a new wait (the old one ended), not a lost one.
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("red, a moved head and a timeout wake the caller and start nothing", () =>
    Effect.gen(function* () {
      for (const end of ["failed", "moved", "timeout"] as const) {
        yield* TestClock.setTime(NOW);
        const filePath = yield* storePath;
        const pr = scriptedPrJudge();
        const send = recordingDelivery();
        const action = recordingAction();
        const service = yield* make(filePath, pr.judge, send.delivery, action.action);
        yield* registerAfterCi(service);
        if (end === "timeout") {
          yield* TestClock.adjust(61 * 60_000);
        } else {
          pr.answer({ type: "met", outcome: end, detail: `${end}.` });
          yield* TestClock.adjust(130_000);
        }
        yield* service.runDue();
        yield* service.deliverDue();
        assert.strictEqual(send.sent.length, 1, end);
        assert.include(send.sent[0]!.text, end, end);
        assert.include(send.sent[0]!.text, "No review lens starts for this head", end);
        assert.strictEqual(action.runs.length, 0, end);
      }
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("a restart after the send but before it was recorded runs the action then", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const filePath = yield* storePath;
      const pr = scriptedPrJudge();
      const send = recordingDelivery();
      const before = recordingAction();
      const first = yield* make(filePath, pr.judge, send.delivery, before.action);
      yield* registerAfterCi(first);
      pr.answer(passed);
      send.losing(true);
      yield* TestClock.adjust(130_000);
      yield* first.runDue();
      // The message is in the thread, the answer was lost: nothing ran and the wait is open.
      assert.strictEqual(send.sent.length, 1);
      assert.strictEqual(before.runs.length, 0);

      // A new server over the same store file.
      send.losing(false);
      const after = recordingAction();
      const second = yield* make(filePath, pr.judge, send.delivery, after.action);
      yield* TestClock.adjust(130_000);
      yield* second.deliverDue();
      assert.strictEqual(send.sent.length, 1, "the wake is not sent twice");
      assert.strictEqual(after.runs.length, 1, "the action runs after the restart");
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("not ready yet (just started): nothing is sent until the action can run", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const filePath = yield* storePath;
      const pr = scriptedPrJudge();
      const send = recordingDelivery();
      const action = recordingAction();
      action.setReady(false);
      const service = yield* make(filePath, pr.judge, send.delivery, action.action);
      yield* registerAfterCi(service);
      pr.answer(passed);
      yield* TestClock.adjust(130_000);
      yield* service.runDue();
      assert.strictEqual(send.sent.length, 0);
      assert.strictEqual(action.runs.length, 0);

      action.setReady(true);
      yield* TestClock.adjust(130_000);
      yield* service.runDue();
      assert.strictEqual(send.sent.length, 1);
      assert.strictEqual(action.runs.length, 1);
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("an action that could not start the lenses tells the thread why, once", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const filePath = yield* storePath;
      const pr = scriptedPrJudge();
      const send = recordingDelivery();
      const action = recordingAction();
      action.setAnswer("lens_start refused #7: a read of this head would be review round 5");
      const service = yield* make(filePath, pr.judge, send.delivery, action.action);
      yield* registerAfterCi(service);
      pr.answer(passed);
      yield* TestClock.adjust(130_000);
      yield* service.runDue();
      yield* TestClock.adjust(130_000);
      yield* service.runDue();
      assert.strictEqual(action.runs.length, 1);
      assert.strictEqual(send.sent.length, 2);
      assert.include(send.sent[1]!.text, "review round 5");
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("the reason not sent yet: the delivery is tried again, the wake not twice", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const filePath = yield* storePath;
      const pr = scriptedPrJudge();
      const send = recordingDelivery();
      send.refuseOnce("-action");
      const action = recordingAction();
      action.setAnswer("lens_start started nothing: no run");
      const service = yield* make(filePath, pr.judge, send.delivery, action.action);
      yield* registerAfterCi(service);
      pr.answer(passed);
      yield* TestClock.adjust(130_000);
      yield* service.runDue();
      assert.strictEqual(send.sent.length, 1, "the wake only: the reason was refused");
      yield* TestClock.adjust(130_000);
      yield* service.runDue();
      yield* service.deliverDue();
      assert.strictEqual(send.sent.length, 2);
      assert.include(send.sent[1]!.text, "no run");
      // The action ran again (it is idempotent); the wake was found, not sent twice.
      assert.strictEqual(action.runs.length, 2);
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("a narrowed wait the store could not read back is refused, never stored", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const filePath = yield* storePath;
      const pr = scriptedPrJudge();
      const send = recordingDelivery();
      const service = yield* make(filePath, pr.judge, send.delivery, recordingAction().action);
      for (const narrow of [
        { checks: [], head: HEAD },
        { checks: ["ci-result"], head: "not-a-sha" },
      ]) {
        const refused = yield* service
          .register({
            threadId: THREAD,
            projectId: PROJECT,
            input: { kind: "pr", pr: { number: 7, repository: "o/r" } },
            narrow,
            action: "lens_start",
          })
          .pipe(Effect.flip);
        assert.strictEqual(refused.code, "invalid");
      }
      assert.deepStrictEqual(pr.asked, []);
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect(
    "an action that does not finish in time: the sweep goes on, the delivery runs again later",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const filePath = yield* storePath;
        const pr = scriptedPrJudge();
        const send = recordingDelivery();
        const action = recordingAction();
        action.setHang(true);
        const service = yield* make(filePath, pr.judge, send.delivery, action.action);
        yield* registerAfterCi(service);
        pr.answer(passed);
        yield* TestClock.adjust(130_000);
        const sweep = yield* Effect.forkChild(service.runDue());
        // Once the action hangs in the sweep, pass its limit.
        yield* Deferred.await(action.ran);
        yield* TestClock.adjust(Duration.toMillis(WaitService.WAIT_ACTION_LIMIT) + 1_000);
        yield* Fiber.join(sweep);
        assert.strictEqual(send.sent.length, 1, "the wake is in the thread");
        assert.strictEqual(action.runs.length, 1);
        // Not written as delivered: the next delivery runs the action again, the wake is found.
        action.setHang(false);
        yield* TestClock.adjust(130_000);
        yield* service.deliverDue();
        assert.strictEqual(action.runs.length, 2);
        assert.strictEqual(send.sent.length, 1);
      }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it.effect("a server without the action: the wait is kept, never delivered half", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const filePath = yield* storePath;
      const pr = scriptedPrJudge();
      const send = recordingDelivery();
      const service = yield* make(filePath, pr.judge, send.delivery);
      yield* registerAfterCi(service);
      pr.answer(passed);
      yield* TestClock.adjust(130_000);
      yield* service.runDue();
      assert.strictEqual(send.sent.length, 0);
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );
});
