// @effect-diagnostics nodeBuiltinImport:off
// Fork: lens_start's plan, prompt, refusals and request ids (lensStart.ts). It replays the CI gate's own shared vectors
// (one source, never copied), so its plan and rounds cannot drift from what the gate requires; a vector it does not
// run fails the count checks below.
import * as NodeFS from "node:fs";
import { effectiveReviewRules } from "@t3tools/contracts";
import { assert, describe, it } from "@effect/vitest";

import { lensKey, lensScan, type LensPost } from "./lensComment.ts";
import {
  FALLBACK_LENS,
  FALLBACK_REVIEW,
  LENS_START_MAX_PRS,
  confirmLensStart,
  lensRequestId,
  planLensStart,
  startRounds,
  type LensStartInput,
  type LensStartPullRequest,
} from "./lensStart.ts";
import { type ReviewBlock } from "./plan.ts";
import { requiredLenses, roundMarker } from "./rounds.ts";

const V = JSON.parse(
  NodeFS.readFileSync(
    new URL("../../../../../fork-ci/actions/pr-queue/review.vectors.json", import.meta.url),
    "utf8",
  ),
);
const configs = V.configs as Record<string, ReviewBlock>;
const basic = configs.basic!;
const ON = effectiveReviewRules({ preset: "recommended" });
if (ON.preset !== "recommended") throw new Error("the recommended preset reads as off");
const REPO = "owner/repo";
const [A, B, C, D, E, F] = ["a", "b", "c", "d", "e", "f"].map((x) => x.repeat(40)) as [
  string,
  string,
  string,
  string,
  string,
  string,
];

const report = (lens: string, sha: string): LensPost => ({
  body: `lens: ${lens} · reviewer: x · read at: ${sha}\nstate: fine\nblocker: none\nmajor: none`,
  edited: false,
  hidden: false,
});
/** Every lens prompt of a config, read at the base. */
const promptsOf = (review: ReviewBlock) =>
  Object.fromEntries(Object.values(review.lenses).map((l) => [l.prompt, `Look at ${l.prompt}.`]));

function pull(number: number, over: Partial<LensStartPullRequest> = {}): LensStartPullRequest {
  return { number, state: "open", head: A, changes: [["M", "src/x.ts"]], posts: [], ...over };
}

function input(over: Partial<LensStartInput> & { pullList?: LensStartPullRequest[] }) {
  const { pullList = [pull(1)], ...rest } = over;
  return {
    rules: ON,
    repo: REPO,
    review: basic,
    prompts: promptsOf(basic),
    overridePrefix: V.override_prefix,
    prs: pullList.map((p) => ({ number: p.number })),
    pulls: new Map(pullList.map((p) => [p.number, p])),
    ...rest,
  } satisfies LensStartInput;
}

const lensesOf = (plan: ReturnType<typeof planLensStart>) => plan.starts.map((s) => s.lens);

describe("lens_start on the shared vectors", () => {
  it("every case: a first read starts each lens the gate requires, one child each, nothing more", () => {
    let ran = 0;
    for (const c of V.cases) {
      const review = configs[c.config]!;
      const plan = planLensStart(
        input({ review, prompts: promptsOf(review), pullList: [pull(7, { changes: c.changes })] }),
      );
      assert.deepStrictEqual(plan.refused, [], c.name);
      assert.deepStrictEqual(lensesOf(plan), c.lenses, c.name);
      for (const s of plan.starts)
        assert.deepStrictEqual(s.prs, [{ number: 7, head: A, round: 1 }]);
      ran++;
    }
    assert.strictEqual(ran, V.cases.length);
    assert.isAtLeast(ran, 28);
  });

  it("every round: a read of an already-read head is that round, a new head at most one more", () => {
    const fresh = "0123456789abcdef0123456789abcdef01234567";
    let ran = 0;
    for (const r of V.rounds) {
      const review = configs[r.config]!;
      const changes = r.changes as Record<string, string> | undefined;
      const changeOf = changes && ((sha: string) => changes[sha] ?? sha);
      const history = r.history as Array<[string, string | null]>;
      assert.notInclude(
        history.map(([, sha]) => sha),
        fresh,
      );
      const last = history.findLast(([, sha]) => sha !== null)?.[1];
      if (last)
        assert.strictEqual(
          startRounds(review, history, last, changeOf).round,
          r.expect.round,
          r.name,
        );
      const next = startRounds(review, history, fresh, changeOf).round;
      const marked = history.some(([name]) => roundMarker(name) >= next);
      assert.isTrue(next === r.expect.round + 1 || (marked && next === r.expect.round), r.name);
      ran++;
    }
    assert.strictEqual(ran, V.rounds.length);
    assert.isAtLeast(ran, 28);
  });

  it("every scan: a lens is not started again exactly when the gate reads its newest entry at the head", () => {
    let ran = 0;
    let read = 0;
    for (const v of V.scans) {
      const posts = (v.posts as Array<{ body: string; edited?: boolean; hidden?: boolean }>).map(
        (p) => ({ body: p.body, edited: p.edited ?? false, hidden: p.hidden ?? false }),
      );
      const entries = v.entries as Record<string, [string | null, string | null]>;
      const history = v.history as Array<[string, string | null]>;
      const heads = new Set([F, ...history.flatMap(([, sha]) => (sha ? [sha.toLowerCase()] : []))]);
      for (const head of heads) {
        if (!/^[0-9a-f]{40}$/u.test(head)) continue;
        const plan = planLensStart(input({ pullList: [pull(3, { head, posts })] }));
        const rounds = startRounds(basic, lensScan(posts, V.override_prefix).history, head);
        if (rounds.round > basic.rounds.max) {
          assert.lengthOf(plan.refused, 1, v.name);
          continue;
        }
        const required = requiredLenses(
          basic,
          { weight: "normal", lenses: ["code"], hits: [], protected: [] },
          rounds,
        );
        for (const [key, name] of required) {
          const gateRead = JSON.stringify(entries[key]) === JSON.stringify([head, null]);
          const skipped = plan.alreadyRead.some((x) => x.lens === name);
          assert.strictEqual(skipped, gateRead, `${v.name} at ${head}: ${key}`);
          assert.strictEqual(lensesOf(plan).includes(name), !gateRead, `${v.name} at ${head}`);
          if (gateRead) read++;
        }
      }
      ran++;
    }
    assert.strictEqual(ran, V.scans.length);
    assert.isAtLeast(ran, 31);
    assert.isAtLeast(read, 1);
  });
});

describe("lens_start answers the state question", () => {
  it("runs again: the same call twice is the same plan, the same ids, the same prompts", () => {
    const make = () =>
      planLensStart(
        input({
          pullList: [pull(1, { posts: [report("code", B)] }), pull(2, { head: C })],
        }),
      );
    const one = make();
    assert.deepStrictEqual(make(), one);
    assert.deepStrictEqual(
      planLensStart(
        input({ pullList: [pull(2, { head: C }), pull(1, { posts: [report("code", B)] })] }),
      ).starts.map((s) => s.requestId),
      one.starts.map((s) => s.requestId),
    );
  });

  it("runs again after a lens reported: that lens is already read, the rest still start", () => {
    const plan = planLensStart(
      input({
        pullList: [
          pull(1, { changes: [["M", "src/storage/x.sql"]], posts: [report("data-1", A)] }),
        ],
      }),
    );
    assert.deepStrictEqual(lensesOf(plan), ["data-2"]);
    assert.deepStrictEqual(plan.alreadyRead, [{ pr: 1, lens: "data-1" }]);
  });

  it("a report that reads nothing (edited, or of another head) does not count as read", () => {
    const edited = { ...report("code", A), edited: true };
    assert.deepStrictEqual(
      lensesOf(planLensStart(input({ pullList: [pull(1, { posts: [edited] })] }))),
      ["code"],
    );
    assert.deepStrictEqual(
      lensesOf(planLensStart(input({ pullList: [pull(1, { posts: [report("code", B)] })] }))),
      ["code"],
    );
    const short = report("code", A.slice(0, 7));
    assert.deepStrictEqual(
      lensesOf(planLensStart(input({ pullList: [pull(1, { posts: [short] })] }))),
      ["code"],
    );
  });

  it("a short read-at sha never counts as reading the head, even of the same change", () => {
    const short = report("code", A.slice(0, 7));
    const plan = planLensStart(
      input({ pullList: [pull(1, { posts: [short], changeOf: () => "same" })] }),
    );
    assert.deepStrictEqual(lensesOf(plan), ["code"]);
  });

  it("a hidden report of this head is no earlier read in the prompt", () => {
    const hidden = { ...report("code", A), hidden: true };
    const plan = planLensStart(
      input({ pullList: [pull(1, { posts: [report("code", B), hidden] })] }),
    );
    assert.include(
      plan.starts[0]!.prompt,
      `read it before at ${B}: check each earlier finding first, then the changes since ${B}.`,
    );
  });

  it("a read before a clean base merge still read the head when the change is the same", () => {
    const plan = planLensStart(
      input({
        pullList: [pull(1, { head: B, posts: [report("code", A)], changeOf: () => "same" })],
      }),
    );
    assert.deepStrictEqual(lensesOf(plan), []);
    assert.deepStrictEqual(plan.alreadyRead, [{ pr: 1, lens: "code" }]);
  });

  it("the head moved since the plan: the child is refused, call again plans the new head", () => {
    const plan = planLensStart(input({ pullList: [pull(1), pull(2, { head: B })] }));
    assert.lengthOf(plan.starts, 1);
    const moved = confirmLensStart(
      plan,
      new Map([
        [1, C],
        [2, B],
      ]),
    );
    assert.deepStrictEqual(moved.starts, []);
    assert.include(moved.refused.find((r) => r.pr === 1)!.reason, `moved from ${A} to ${C}`);
    assert.isTrue(moved.refused.some((r) => r.pr === null && r.reason.includes("#1 #2")));
    const kept = confirmLensStart(
      plan,
      new Map([
        [1, A.toUpperCase()],
        [2, B],
      ]),
    );
    assert.strictEqual(kept, plan);
    const closed = confirmLensStart(
      plan,
      new Map([
        [1, null],
        [2, B],
      ]),
    );
    assert.deepStrictEqual(closed.starts, []);
    assert.include(closed.refused.find((r) => r.pr === 1)!.reason, "no longer open");
    const again = planLensStart(input({ pullList: [pull(1, { head: C }), pull(2, { head: B })] }));
    assert.notStrictEqual(again.starts[0]!.requestId, plan.starts[0]!.requestId);
  });

  it("a moved head only stops the children that read it", () => {
    const plan = planLensStart(
      input({
        pullList: [
          pull(1, { changes: [["M", "src/storage/x.sql"]] }),
          pull(2, { head: B, posts: [report("data-1", B)], changes: [["M", "src/storage/y.sql"]] }),
        ],
      }),
    );
    // data-1 reads #1 only, data-2 reads both.
    assert.deepStrictEqual(
      plan.starts.map((s) => [s.lens, s.prs.map((r) => r.number)]),
      [
        ["data-1", [1]],
        ["data-2", [1, 2]],
      ],
    );
    const got = confirmLensStart(
      plan,
      new Map([
        [1, A],
        [2, C],
      ]),
    );
    assert.deepStrictEqual(lensesOf(got), ["data-1"]);
  });

  it("the caller's head is not GitHub's: refused, no read of a head nobody asked for", () => {
    const plan = planLensStart({ ...input({}), prs: [{ number: 1, head: B }] });
    assert.deepStrictEqual(plan.starts, []);
    assert.include(plan.refused[0]!.reason, `its head is ${A}, not ${B}`);
    assert.lengthOf(
      planLensStart({ ...input({}), prs: [{ number: 1, head: A.toUpperCase() }] }).starts,
      1,
    );
  });

  it("old or empty settings: unset, empty, off, unreadable or a project's off all start nothing", () => {
    for (const rules of [
      effectiveReviewRules(undefined),
      effectiveReviewRules({ preset: "off" }),
      effectiveReviewRules({ preset: "recommended", unreadable: { preset: "on" } }),
      effectiveReviewRules({ preset: "recommended" }, { preset: "off" }),
    ]) {
      const plan = planLensStart(input({ rules }));
      assert.deepStrictEqual(plan.starts, []);
      assert.deepStrictEqual(
        plan.refused.map((r) => r.pr),
        [null],
      );
      assert.include(plan.refused[0]!.reason, "review rules are off");
    }
  });

  it("a round already at the limit: the head read in round 4 still gets its missing lens; round 5 never starts", () => {
    const four = [report("code", A), report("code", B), report("code", C), report("code", D)];
    const at = planLensStart(
      input({
        pullList: [pull(1, { head: D, posts: four, changes: [["M", "src/storage/x.sql"]] })],
      }),
    );
    assert.deepStrictEqual(at.refused, []);
    assert.deepStrictEqual(lensesOf(at), ["data-1", "data-2", "approach"]);
    assert.isTrue(at.starts.every((s) => s.prs[0]!.round === 4));
    const over = planLensStart(input({ pullList: [pull(1, { head: E, posts: four })] }));
    assert.deepStrictEqual(over.starts, []);
    assert.include(over.refused[0]!.reason, "review round 5, past the cap of 4");
    const marked = planLensStart(
      input({ pullList: [pull(1, { head: B, posts: [report("code r5", A)] })] }),
    );
    assert.include(marked.refused[0]!.reason, "review round 5");
  });

  it("a partial batch: a closed, merged or unknown pull request is refused, the rest start", () => {
    const plan = planLensStart({
      ...input({
        pullList: [
          pull(1),
          pull(2, { state: "closed", head: B }),
          pull(3, { state: "merged", head: C }),
          pull(5, { head: E }),
        ],
      }),
      prs: [1, 2, 3, 4, 5].map((number) => ({ number })),
    });
    assert.deepStrictEqual(
      plan.starts.map((s) => s.prs.map((r) => r.number)),
      [[1, 5]],
    );
    assert.deepStrictEqual(
      plan.refused.map((r) => r.pr),
      [2, 3, 4],
    );
    assert.include(plan.refused[0]!.reason, "closed");
    assert.include(plan.refused[2]!.reason, "no pull request #4");
  });

  it("a batch child that reported some pull requests: again, only the missing ones get a new child", () => {
    const first = planLensStart(input({ pullList: [pull(1), pull(2, { head: B })] }));
    const later = planLensStart(
      input({ pullList: [pull(1, { posts: [report("code", A)] }), pull(2, { head: B })] }),
    );
    assert.deepStrictEqual(
      later.starts.map((s) => s.prs.map((r) => r.number)),
      [[2]],
    );
    assert.notStrictEqual(later.starts[0]!.requestId, first.starts[0]!.requestId);
    assert.strictEqual(
      later.starts[0]!.requestId,
      lensRequestId(REPO, "code", [{ number: 2, head: B }]),
    );
  });

  it("a person's override for the head: nothing starts, a new lens comment would end it", () => {
    const plan = planLensStart(input({ pullList: [pull(1, { decided: true })] }));
    assert.deepStrictEqual(plan.starts, []);
    assert.include(plan.refused[0]!.reason, "override");
  });
});

describe("lens_start's plan, prompt and refusals", () => {
  it("no plan: changes it cannot read, a path no rule takes, or a prompt not read are refused, never too few lenses", () => {
    const noCatchAll = { ...basic, rules: basic.rules.filter((r) => r.name !== "code") };
    for (const [over, words] of [
      [{ pullList: [pull(1, { changes: [["X", "a"]] })] }, "unknown change status"],
      [{ review: noCatchAll, pullList: [pull(1)] }, "no rule matches"],
      [{ prompts: {} }, "could not be read at the base"],
    ] as const) {
      const plan = planLensStart(input(over as never));
      assert.deepStrictEqual(plan.starts, []);
      assert.include(plan.refused[0]!.reason, "no plan");
      assert.include(plan.refused[0]!.reason, words);
    }
    const own: ReviewBlock = {
      ...basic,
      lenses: { ...basic.lenses, "data-2": { prompt: ".github/lenses/data-2.md" } },
    };
    const one = { ...promptsOf(basic) };
    const storage = [["M", "src/storage/x.sql"]];
    const plan = planLensStart(
      input({ review: own, prompts: one, pullList: [pull(1, { changes: storage })] }),
    );
    assert.deepStrictEqual(plan.starts, []);
    assert.include(plan.refused[0]!.reason, "data-2 (.github/lenses/data-2.md)");
    // A lens already read needs no prompt.
    const read = planLensStart(
      input({
        review: own,
        prompts: one,
        pullList: [pull(1, { changes: storage, posts: [report("data-2", A)] })],
      }),
    );
    assert.deepStrictEqual(lensesOf(read), ["data-1"]);
  });

  it("refuses a bad call whole", () => {
    for (const [over, words] of [
      [{ repo: "not a repo" }, "not owner/name"],
      [{ prs: [] }, "at least one"],
      [{ prs: [{ number: 0 }] }, "not a pull request number"],
      [
        {
          prs: [
            { number: 1, head: A },
            { number: 1, head: B },
          ],
        },
        "named twice",
      ],
      [
        { prs: Array.from({ length: LENS_START_MAX_PRS + 1 }, (_, i) => ({ number: i + 1 })) },
        `at most ${LENS_START_MAX_PRS}`,
      ],
    ] as const) {
      const plan = planLensStart({ ...input({}), ...over });
      assert.deepStrictEqual(plan.starts, []);
      assert.deepStrictEqual(
        plan.refused.map((r) => r.pr),
        [null],
      );
      assert.include(plan.refused[0]!.reason, words);
    }
    assert.lengthOf(planLensStart({ ...input({}), prs: [{ number: 1 }, { number: 1 }] }).starts, 1);
    assert.include(
      planLensStart(input({ pullList: [pull(1, { head: "abc" })] })).refused[0]!.reason,
      "not a full sha",
    );
  });

  it("from round 3 the approach lens joins, with its own runner", () => {
    const plan = planLensStart(
      input({ pullList: [pull(1, { head: C, posts: [report("code", A), report("code", B)] })] }),
    );
    assert.deepStrictEqual(lensesOf(plan), ["code", "approach"]);
    assert.deepStrictEqual(
      plan.starts.map((s) => s.runner.model),
      ["claude-opus-5-5", "claude-fable-5-2"],
    );
    const two = planLensStart(
      input({ pullList: [pull(1, { head: B, posts: [report("code", A)] })] }),
    );
    assert.deepStrictEqual(lensesOf(two), ["code"]);
  });

  it("a runner set for a lens wins over the preset's", () => {
    const rules = effectiveReviewRules({
      preset: "recommended",
      lensRunners: { code: { driver: ON.runner.driver, model: "m", effort: "xhigh" } },
    });
    const plan = planLensStart(input({ rules }));
    assert.deepStrictEqual(plan.starts[0]!.runner, {
      driver: ON.runner.driver,
      model: "m",
      effort: "xhigh",
    });
  });

  it("a repo without a review block: one lens `review`, said in a note, critical paths not guessed", () => {
    const plan = planLensStart(
      input({
        review: null,
        prompts: {},
        pullList: [pull(1, { changes: [["M", ".github/workflows/ci.yml"]] })],
      }),
    );
    assert.deepStrictEqual(plan.refused, []);
    assert.deepStrictEqual(lensesOf(plan), [FALLBACK_LENS]);
    assert.include(plan.notes[0], "no `review` block");
    assert.include(plan.starts[0]!.prompt, "correctness");
    const late = planLensStart(
      input({
        review: null,
        prompts: {},
        pullList: [pull(1, { head: C, posts: [report("review", A), report("review", B)] })],
      }),
    );
    assert.deepStrictEqual(lensesOf(late), [FALLBACK_LENS, FALLBACK_REVIEW.rounds.approach_lens]);
    const over = [A, B, C, D].map((sha) => report("review", sha));
    assert.include(
      planLensStart(input({ review: null, pullList: [pull(1, { head: E, posts: over })] }))
        .refused[0]!.reason,
      "past the cap of 4",
    );
  });

  it("the prompt: read only, the lens text, the state question word for word, the report rules and earlier reads", () => {
    const plan = planLensStart(
      input({ pullList: [pull(4, { head: B, posts: [report("code", A)] })] }),
    );
    const prompt = plan.starts[0]!.prompt;
    assert.include(prompt, "Read only");
    assert.include(prompt, `Look at ${basic.lenses.code!.prompt}.`);
    assert.include(prompt, ON.stateQuestion);
    assert.include(prompt, "line starting `state:`");
    assert.include(prompt, "`blocker:`, `major:` or `minor:`");
    assert.include(prompt, `under ${ON.reportMaxChars} characters`);
    assert.include(prompt, `#4 at head ${B}: review round 2.`);
    assert.include(
      prompt,
      `read it before at ${A}: check each earlier finding first, then the changes since ${A}`,
    );
    assert.include(prompt, "`lens_report`, for the head above");
    const custom = effectiveReviewRules({
      preset: "recommended",
      stateQuestion: "What if it runs twice or finds old data?",
    });
    assert.include(
      planLensStart(input({ rules: custom })).starts[0]!.prompt,
      "What if it runs twice or finds old data?",
    );
  });

  it("a batch: one child per lens reads every pull request needing it, one report per pull request with its head", () => {
    const plan = planLensStart(
      input({
        pullList: [
          pull(1),
          pull(2, { head: B, changes: [["M", "src/storage/x.sql"]] }),
          pull(3, { head: C, changes: [["M", "docs/a.md"]] }),
        ],
      }),
    );
    assert.deepStrictEqual(
      plan.starts.map((s) => [s.lens, s.prs.map((r) => r.number)]),
      [
        ["code", [1]],
        ["data-1", [2]],
        ["data-2", [2]],
      ],
    );
    const both = planLensStart(input({ pullList: [pull(1), pull(2, { head: B })] }));
    assert.lengthOf(both.starts, 1);
    const child = both.starts[0]!;
    assert.include(child.prompt, `#1 at head ${A}`);
    assert.include(child.prompt, `#2 at head ${B}`);
    assert.include(child.prompt, "one report per pull request");
    assert.strictEqual(child.title, "lens code #1 #2");
    assert.strictEqual(child.key, lensKey("code"));
  });

  it("request ids: the same lens and heads give the same id, in any order; each fits delegate_task", () => {
    const two = [
      { number: 1, head: A },
      { number: 2, head: B },
    ];
    assert.strictEqual(
      lensRequestId(REPO, "code", two),
      lensRequestId(REPO, "code", two.toReversed()),
    );
    assert.notStrictEqual(
      lensRequestId(REPO, "code", two),
      lensRequestId(REPO, "code", [two[0]!, { number: 2, head: C }]),
    );
    assert.notStrictEqual(lensRequestId(REPO, "code", two), lensRequestId(REPO, "data-1", two));
    assert.notStrictEqual(
      lensRequestId(REPO, "code", two),
      lensRequestId("owner/other", "code", two),
    );
    assert.strictEqual(lensRequestId(REPO, "code", [two[0]!]), `lens/${REPO}#1/${A}/code`);
    assert.strictEqual(
      lensRequestId(REPO, "code", [two[0]!], 2),
      `lens/${REPO}#1/${A}/code/again-2`,
    );
    const long = lensRequestId(`o/${"r".repeat(200)}`, "x".repeat(40), [two[0]!]);
    assert.isAtMost(long.length, 256);
    assert.notStrictEqual(long, lensRequestId(`o/${"r".repeat(200)}`, "y".repeat(40), [two[0]!]));
    assert.strictEqual(
      lensRequestId(REPO, "code review", [two[0]!]),
      `lens/${REPO}#1/${A}/code%20review`,
    );
  });
});
