// Fork: the `lens_start` tool's run (lensStartRun.ts): what it reads, which children it starts through the
// delegate_task path, its refusals, a repeat, and `afterCi` (a wait on the CI checks the config names).
import { effectiveReviewRules, type EffectiveReviewRules } from "@t3tools/contracts";
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";

import {
  ciChecksOf,
  delegateInputOf,
  lensStartNamespace,
  parseQueueConfig,
  runLensStart,
  type CheckoutState,
  type LensStartWorld,
  type PullRead,
  type RawPost,
  type StartAnswer,
  type StoredChild,
  type WaitForCiAnswer,
} from "./lensStartRun.ts";
import type { LensStartChild } from "./lensStart.ts";

const ON = effectiveReviewRules({ preset: "recommended" });
const HEAD = "a".repeat(40);
const NEWER = "b".repeat(40);
const OLD = ["1", "2", "3", "4"].map((x) => x.repeat(40));
const LENS_PROMPT = "Read the code paths the change touches, and their callers.";

const CONFIG = {
  required_checks: ["ci-result", "head_check"],
  merge_method: "squash",
  bot_logins: ["robot"],
  lens_author_associations: ["OWNER"],
  override: { prefix: "override for head", author_association: "OWNER" },
  ready_command: "x",
  push_command: "git push",
  lens_format_doc: "doc",
  head_check: "head_check",
  review: {
    version: 1,
    mode: "enforce",
    rules: [
      { name: "gate", weight: "critical", paths: [".github/**"], lenses: ["code", "state"] },
      { name: "code", weight: "normal", paths: ["**"], lenses: ["code"] },
    ],
    lenses: {
      code: { prompt: "lenses/code.md" },
      state: { prompt: "lenses/state.md" },
      approach: { prompt: "lenses/approach.md" },
    },
    min_lenses: { critical: 2, normal: 1, light: 0 },
    rounds: { approach_at: 3, max: 4, approach_lens: "approach" },
    findings: true,
    override_authors: [],
    state_line: false,
  },
};

const lensPost = (
  lens: string,
  sha: string,
  when: string,
  extra: Partial<RawPost> = {},
): RawPost => ({
  body: `lens: ${lens} · reviewer: x · read at: ${sha}\nstate: fine\nblocker: none\nmajor: none`,
  when,
  url: `https://example.test/c/${lens}-${sha.slice(0, 4)}`,
  login: "owner",
  bot: false,
  association: "OWNER",
  edited: false,
  hidden: false,
  ...extra,
});

interface Scene {
  rules?: EffectiveReviewRules;
  pull?: Partial<PullRead> | null;
  config?: unknown;
  files?: Record<string, string>;
  changes?: ReadonlyArray<ReadonlyArray<string>>;
  checkout?: CheckoutState | null;
  children?: StoredChild[];
  running?: (child: StoredChild) => boolean;
  start?: (child: LensStartChild) => StartAnswer;
  waitForCi?: () => WaitForCiAnswer;
  /** The head GitHub names at the second read (just before the start). */
  headLater?: string;
}

/** A world over one made-up repo; records what it started and every wait it asked for. */
function world(scene: Scene = {}) {
  const started: LensStartChild[] = [];
  const waits: Array<{ number: number; head: string; checks: ReadonlyArray<string> }> = [];
  const children: StoredChild[] = [...(scene.children ?? [])];
  let reads = 0;
  const pull: PullRead | null =
    scene.pull === null
      ? null
      : {
          number: 7,
          state: "open",
          head: HEAD,
          base: "main",
          headBranch: "feat/x",
          posts: [],
          ...scene.pull,
        };
  const files: Record<string, string> = {
    ".github/pr-queue.json": JSON.stringify(scene.config ?? CONFIG),
    "lenses/code.md": LENS_PROMPT,
    "lenses/state.md": "Ask the state question.",
    "lenses/approach.md": "Judge the approach.",
    ...scene.files,
  };
  const w: LensStartWorld = {
    rules: scene.rules ?? ON,
    repo: "owner/repo",
    readPull: (number) =>
      Effect.sync(() => {
        reads += 1;
        if (pull === null || number !== pull.number) return null;
        return reads > 1 && scene.headLater !== undefined
          ? { ...pull, head: scene.headLater }
          : pull;
      }),
    readFile: (ref, path) =>
      Effect.sync(() => {
        assert.strictEqual(ref, "main", "every file is read at the base");
        return files[path] ?? null;
      }),
    readChanges: () => Effect.succeed(scene.changes ?? [["M", "src/x.ts"]]),
    checkout: () => Effect.succeed(scene.checkout === undefined ? null : scene.checkout),
    children: {
      list: Effect.sync(() => [...children]),
      add: (child) => Effect.sync(() => void children.push(child)),
    },
    childRunning: (child) => Effect.succeed(scene.running?.(child) ?? true),
    start: (child) =>
      Effect.sync(() => {
        const answer = scene.start?.(child) ?? {
          type: "started" as const,
          taskId: `task-${started.length + 1}`,
          childThreadId: `child-${started.length + 1}`,
        };
        if (answer.type === "started") started.push(child);
        return answer;
      }),
    waitForCi: (args) =>
      Effect.sync(() => {
        waits.push(args);
        return scene.waitForCi?.() ?? { type: "waiting", waitId: "w_aaaaaaaaaa" };
      }),
  };
  return { w, started, waits, children };
}

describe("lens_start: starts", () => {
  it.effect(
    "plans from the config, prompts and comments at the base and starts one review child per lens",
    () =>
      Effect.gen(function* () {
        const { w, started, children } = world({ changes: [["M", ".github/ci.yml"]] });
        const result = yield* runLensStart(w, { pr: 7 });
        assert.strictEqual(result.state, "started");
        assert.deepStrictEqual(started.map((c) => c.lens).toSorted(), ["code", "state"]);
        const code = started.find((c) => c.lens === "code")!;
        assert.strictEqual(code.requestId, `lens/owner/repo#7/${HEAD}/code`);
        // The server's prompt: the frame, the base's lens prompt, the state question word for word.
        assert.include(code.prompt, "Read only");
        assert.include(code.prompt, LENS_PROMPT);
        assert.include(code.prompt, ON.preset === "recommended" ? ON.stateQuestion : "");
        assert.include(code.prompt, "Answer it in a line starting `state:`.");
        assert.include(code.prompt, "starting `blocker:`, `major:` or `minor:`");
        assert.include(code.prompt, "review round 1");
        // One child per lens, never two lenses in one child.
        assert.isTrue(started.every((c) => c.prs.length === 1));
        assert.strictEqual(children.length, 2);
        assert.deepStrictEqual(children[0]!.prs, [{ number: 7, head: HEAD }]);
      }),
  );

  it("starts each child through delegate_task as a review child, on the lens's runner", () => {
    if (ON.preset !== "recommended") throw new Error("off");
    const input = delegateInputOf({
      requestId: "lens/owner/repo#7/x/code",
      attempt: 1,
      replaces: [],
      lens: "code",
      key: "code",
      runner: ON.runner,
      title: "lens code #7",
      prompt: "p",
      prs: [{ number: 7, head: HEAD, round: 1 }],
    });
    assert.strictEqual(input.role, "review");
    assert.strictEqual(input.clientRequestId, "lens/owner/repo#7/x/code");
    assert.strictEqual(input.task, "p");
    assert.strictEqual(input.mode, "async");
    assert.deepStrictEqual(input.target, {
      driverKind: ON.runner.driver,
      model: ON.runner.model,
      options: [{ id: "effort", value: ON.runner.effort }],
    });
    // One request namespace per orchestrator thread, whatever its session: a repeat is the same task.
    assert.strictEqual(lensStartNamespace("thread-1"), lensStartNamespace("thread-1"));
  });

  it.effect(
    "from round 2 the prompt asks to check each earlier finding, then the changes since",
    () =>
      Effect.gen(function* () {
        const { w, started } = world({
          pull: { posts: [lensPost("code", OLD[0]!, "2026-11-20T00:00:00Z")] },
        });
        yield* runLensStart(w, { pr: 7 });
        assert.include(started[0]!.prompt, "review round 2");
        assert.include(
          started[0]!.prompt,
          "check each earlier finding first, then the changes since",
        );
      }),
  );
});

describe("lens_start: runs again", () => {
  it.effect("a second call for the same head returns the running child and starts nothing", () =>
    Effect.gen(function* () {
      const first = world();
      yield* runLensStart(first.w, { pr: 7 });
      assert.strictEqual(first.started.length, 1);
      const again = world({ children: first.children, running: () => true });
      const result = yield* runLensStart(again.w, { pr: 7 });
      assert.strictEqual(again.started.length, 0);
      assert.strictEqual(result.state, "nothing");
      assert.deepStrictEqual(
        result.running.map((r) => r.requestId),
        [`lens/owner/repo#7/${HEAD}/code`],
      );
    }),
  );

  it.effect("a lens whose newest read is this head is not started again", () =>
    Effect.gen(function* () {
      const { w, started } = world({
        pull: { posts: [lensPost("code", HEAD, "2026-11-22T00:00:00Z")] },
      });
      const result = yield* runLensStart(w, { pr: 7 });
      assert.strictEqual(started.length, 0);
      assert.deepStrictEqual(result.alreadyRead, ["code"]);
    }),
  );

  it.effect(
    "a child that ended without a read starts again as the next attempt, with a new id",
    () =>
      Effect.gen(function* () {
        const first = world();
        yield* runLensStart(first.w, { pr: 7 });
        const again = world({ children: first.children, running: () => false });
        yield* runLensStart(again.w, { pr: 7 });
        assert.strictEqual(again.started.length, 1);
        assert.strictEqual(again.started[0]!.attempt, 2);
        assert.notStrictEqual(again.started[0]!.requestId, first.started[0]!.requestId);
      }),
  );

  it.effect("old or empty state: no stored children and no comments is a first start", () =>
    Effect.gen(function* () {
      const { w, started } = world({ children: [] });
      const result = yield* runLensStart(w, { pr: 7 });
      assert.strictEqual(result.state, "started");
      assert.strictEqual(started.length, 1);
    }),
  );
});

describe("lens_start: refusals", () => {
  const refused = (scene: Scene, request: { pr: number; head?: string } = { pr: 7 }) =>
    Effect.gen(function* () {
      const { w, started } = world(scene);
      const result = yield* runLensStart(w, request);
      assert.strictEqual(started.length, 0);
      assert.strictEqual(result.state, "refused");
      return result.refused.join(" | ");
    });

  it.effect("review rules off", () =>
    Effect.gen(function* () {
      const why = yield* refused({ rules: effectiveReviewRules(undefined) });
      assert.include(why, "review rules are off");
    }),
  );

  it.effect("a runner whose effort the model policy forbids: nothing more starts on it", () =>
    Effect.gen(function* () {
      let asked = 0;
      const { w, started } = world({
        changes: [["M", ".github/ci.yml"]],
        // The policy refuses the first child; a second on the same runner is never asked.
        start: () =>
          (asked += 1) === 1
            ? {
                type: "refused",
                code: "model_unavailable",
                message: "effort high is not allowed for this model",
              }
            : { type: "started", taskId: "task-x", childThreadId: null },
      });
      const result = yield* runLensStart(w, { pr: 7 });
      assert.strictEqual(asked, 1);
      assert.strictEqual(started.length, 0);
      assert.strictEqual(result.state, "refused");
      assert.lengthOf(result.refused, 2);
      assert.include(result.refused[0]!, "effort high is not allowed");
      assert.include(result.refused[1]!, "its runner");
    }),
  );

  it.effect("a round above rounds.max", () =>
    Effect.gen(function* () {
      const posts = OLD.map((sha, i) => lensPost("code", sha, `2026-11-2${i}T00:00:00Z`));
      const why = yield* refused({ pull: { posts } });
      assert.include(why, "past the cap of 4");
    }),
  );

  it.effect("a dirty checkout of the pull request's branch", () =>
    Effect.gen(function* () {
      const why = yield* refused({
        checkout: { branch: "feat/x", head: HEAD, dirty: true, ahead: false },
      });
      assert.include(why, "uncommitted");
    }),
  );

  it.effect("unpushed commits on the pull request's branch", () =>
    Effect.gen(function* () {
      const why = yield* refused({
        checkout: { branch: "feat/x", head: NEWER, dirty: false, ahead: true },
      });
      assert.include(why, "not pushed");
    }),
  );

  it.effect("a checkout on another branch is not the head's business", () =>
    Effect.gen(function* () {
      const { w, started } = world({
        checkout: { branch: "main", head: NEWER, dirty: true, ahead: true },
      });
      yield* runLensStart(w, { pr: 7 });
      assert.strictEqual(started.length, 1);
    }),
  );

  it.effect("a head that moved since the caller read it, and one that moves before the start", () =>
    Effect.gen(function* () {
      const said = yield* refused({}, { pr: 7, head: NEWER });
      assert.include(said, "read the pull request again");
      const moved = yield* refused({ headLater: NEWER });
      assert.include(moved, "call lens_start again");
    }),
  );

  it.effect("an unknown pull request, and a base without the config", () =>
    Effect.gen(function* () {
      assert.include(yield* refused({ pull: null }), "no pull request #7");
      assert.include(yield* refused({ files: { ".github/pr-queue.json": "{" } }), "cannot be read");
    }),
  );
});

describe("lens_start afterCi", () => {
  it("waits on the config's required checks, the gate's own check left out", () => {
    const config = parseQueueConfig(JSON.stringify(CONFIG));
    if (typeof config === "string") throw new Error(config);
    assert.deepStrictEqual(ciChecksOf(config), ["ci-result"]);
  });

  it.effect("registers a wait for the head's CI and starts nothing yet", () =>
    Effect.gen(function* () {
      const { w, started, waits } = world();
      const result = yield* runLensStart(w, { pr: 7, afterCi: true });
      assert.strictEqual(result.state, "waiting");
      assert.strictEqual(result.waitId, "w_aaaaaaaaaa");
      assert.strictEqual(started.length, 0);
      assert.deepStrictEqual(waits, [{ number: 7, head: HEAD, checks: ["ci-result"] }]);
    }),
  );

  it.effect(
    "a config whose only required check is the gate's own: no CI to wait for, starts now",
    () =>
      Effect.gen(function* () {
        const { w, started, waits } = world({
          config: { ...CONFIG, required_checks: ["head_check"] },
        });
        const result = yield* runLensStart(w, { pr: 7, afterCi: true });
        assert.deepStrictEqual(waits, []);
        assert.strictEqual(result.state, "started");
        assert.strictEqual(started.length, 1);
        assert.isTrue(result.notes.some((note) => note.includes("no CI to wait for")));
      }),
  );

  it.effect("CI already green at the head: starts now", () =>
    Effect.gen(function* () {
      const { w, started } = world({
        waitForCi: () => ({ type: "met", outcome: "passed", detail: "ok" }),
      });
      const result = yield* runLensStart(w, { pr: 7, afterCi: true });
      assert.strictEqual(result.state, "started");
      assert.strictEqual(started.length, 1);
    }),
  );

  it.effect("CI already red: nothing starts and the caller is told why", () =>
    Effect.gen(function* () {
      const { w, started } = world({
        waitForCi: () => ({ type: "met", outcome: "failed", detail: "ci-result failed." }),
      });
      const result = yield* runLensStart(w, { pr: 7, afterCi: true });
      assert.strictEqual(started.length, 0);
      assert.strictEqual(result.state, "nothing");
      assert.include(result.next, "ci-result failed.");
    }),
  );

  it.effect("a call that would be refused is refused at once, before any wait", () =>
    Effect.gen(function* () {
      const posts = OLD.map((sha, i) => lensPost("code", sha, `2026-11-2${i}T00:00:00Z`));
      const { w, waits } = world({ pull: { posts } });
      const result = yield* runLensStart(w, { pr: 7, afterCi: true });
      assert.strictEqual(result.state, "refused");
      assert.deepStrictEqual(waits, []);
    }),
  );

  it.effect("nothing left to read: no wait either", () =>
    Effect.gen(function* () {
      const { w, waits } = world({
        pull: { posts: [lensPost("code", HEAD, "2026-11-22T00:00:00Z")] },
      });
      const result = yield* runLensStart(w, { pr: 7, afterCi: true });
      assert.strictEqual(result.state, "nothing");
      assert.deepStrictEqual(waits, []);
    }),
  );
});

describe("lens_start: which posts count", () => {
  it.effect("a bot's and a stranger's lens-looking comments count for nothing", () =>
    Effect.gen(function* () {
      const { w, started } = world({
        pull: {
          posts: [
            lensPost("code", HEAD, "2026-11-22T00:00:00Z", { login: "robot" }),
            lensPost("code", HEAD, "2026-11-22T00:00:01Z", { association: "NONE" }),
          ],
        },
      });
      const result = yield* runLensStart(w, { pr: 7 });
      assert.strictEqual(started.length, 1);
      assert.deepStrictEqual(result.alreadyRead, []);
    }),
  );

  it.effect("an edited report reads nothing: the lens starts again", () =>
    Effect.gen(function* () {
      const { w, started } = world({
        pull: { posts: [lensPost("code", HEAD, "2026-11-22T00:00:00Z", { edited: true })] },
      });
      yield* runLensStart(w, { pr: 7 });
      assert.strictEqual(started.length, 1);
    }),
  );
});
