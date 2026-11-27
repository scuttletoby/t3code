// @effect-diagnostics nodeBuiltinImport:off
// Fork: lens_start's plan, prompt, refusals and request ids (lensStart.ts). It replays the CI gate's own shared vectors
// (one source, never copied), so its plan and rounds cannot drift from what the gate requires; a vector it does not
// run fails the count checks below.
import * as NodeFS from "node:fs";
import { effectiveReviewRules } from "@t3tools/contracts";
import { assert, describe, it } from "@effect/vitest";

import { NO_HEADER, NO_MARKER, REPOST, lensKey, lensScan } from "./lensComment.ts";
import { readFindings } from "./findings.ts";
import { byCodePoint } from "./pyText.ts";
import { vectorPosts, type VectorPost } from "./vectors.testkit.ts";
import {
  FALLBACK_REVIEW,
  LENS_START_MAX_ATTEMPTS,
  LENS_START_MAX_PRS,
  confirmLensStart,
  earlierChild,
  gateReads,
  gateReadsPass,
  lensRequestId,
  planLensStart,
  startRounds,
  postAgainKeys,
  type LensStartBase,
  type LensStartInput,
  type LensStartPlan,
  type LensStartPost,
  type LensStartPullRequest,
} from "./lensStart.ts";
import { type ReviewBlock } from "./plan.ts";
import { judgeRounds, roundMarker } from "./rounds.ts";

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

const report = (lens: string, sha: string): LensStartPost => ({
  body: `lens: ${lens} · reviewer: x · read at: ${sha}\nstate: fine\nblocker: none\nmajor: none`,
  edited: false,
  hidden: false,
  where: `report ${lens} ${sha}`,
  when: "",
});
/** Every lens prompt of a config, read at the base. */
const promptsOf = (review: ReviewBlock) =>
  Object.fromEntries(Object.values(review.lenses).map((l) => [l.prompt, `Look at ${l.prompt}.`]));

const override = (sha: string): LensStartPost => ({
  body: `${V.override_prefix} ${sha}: a person looked`,
  edited: false,
  hidden: false,
  where: `override ${sha}`,
  when: "",
  override: sha,
});

function pull(number: number, over: Partial<LensStartPullRequest> = {}): LensStartPullRequest {
  return {
    number,
    state: "open",
    head: A,
    base: "main",
    changes: [["M", "src/x.ts"]],
    posts: [],
    ...over,
  };
}

/** One base: its review block (enforce: the path rules name the lenses), prompts and the gate's input. */
const baseOf = (
  review: ReviewBlock | null,
  requiredLenses: ReadonlyArray<string> | null = [],
  prompts: Record<string, string> = review ? promptsOf(review) : {},
): LensStartBase => ({ review, prompts, requiredLenses });

function input(
  over: Partial<LensStartInput> & {
    pullList?: LensStartPullRequest[];
    review?: ReviewBlock | null;
    prompts?: Record<string, string>;
    required?: ReadonlyArray<string> | null;
  },
) {
  const { pullList = [pull(1)], review = basic, prompts, required = [], ...rest } = over;
  return {
    rules: ON,
    repo: REPO,
    bases: new Map([["main", baseOf(review, required, prompts)]]),
    overridePrefix: V.override_prefix,
    prs: pullList.map((p) => ({ number: p.number })),
    pulls: new Map(pullList.map((p) => [p.number, p])),
    ...rest,
  } satisfies LensStartInput;
}

/**
 * The gate's lens verdict, written from its own code (not lensStart.ts): head_check.decide's required lenses,
 * lens_reads (newest entry by [when, index]; a newer override covers it), head_check.verdict (one full sha: the head or
 * `sameChange`) and, in enforce, review_rounds' UNREAD. `changeOf` (the own diff) counts rounds only.
 */
function gatePasses(
  review: ReviewBlock,
  input: ReadonlyArray<string>,
  pathLenses: ReadonlyArray<string>,
  posts: ReadonlyArray<LensStartPost>,
  head: string,
  changeOf: (sha: string) => string = (sha) => sha,
  sameChange: (read: string) => boolean = () => false,
): boolean {
  // Each post's age as a rank: the posts sorted by their time, ties by list place (lens_posts' [when, index]).
  const rank: number[] = [];
  posts
    .map((p, i) => ({ p, i }))
    .toSorted((x, y) => byCodePoint(x.p.when, y.p.when) || x.i - y.i)
    .forEach(({ i }, r) => (rank[i] = r));
  const byAge = posts.map((p, i) => ({ p, i: rank[i]! })).toSorted((x, y) => x.i - y.i);
  const lensPosts = byAge.filter(({ p }) => p.override === undefined);
  const newest = byAge.findLast(({ p }) => p.override !== undefined)?.i ?? -1;
  const people = (review.override_authors ?? []).map((a) => a.toLowerCase());
  const person =
    byAge.findLast(
      ({ p }) =>
        p.override !== undefined &&
        people.includes((p.author ?? "").toLowerCase()) &&
        (p.override === head || sameChange(p.override)),
    )?.i ?? -1;
  const entries: Record<string, { at: number; sha: string | null; body: string }> = {};
  for (const { p, i } of lensPosts) {
    for (const [key, [sha]] of Object.entries(lensScan([p], V.override_prefix).entries)) {
      entries[key] = { at: i, sha, body: p.body };
    }
  }
  const reads = Object.entries(entries).map(([key, { at, sha }]) => [
    key,
    newest >= 0 && at < newest
      ? byAge[newest]!.p.override!
      : sha && /^[0-9a-f]{40}$/u.test(sha)
        ? sha
        : null,
  ]);
  const required = new Set(input.map(lensKey));
  if (review.mode === "enforce") {
    pathLenses.forEach((name) => required.add(lensKey(name)));
    const history = lensScan(
      posts.filter((p) => p.override === undefined),
      V.override_prefix,
    ).history;
    if (judgeRounds(review, history, {}, false, changeOf).approach)
      required.add(lensKey(review.rounds.approach_lens));
    // review_rounds: UNREAD (no full sha, a newer override) or a REPORT with no findings section fails the head.
    const failed = Object.entries(entries).some(([key, { at, sha, body }]) => {
      if (!review.findings || at < person || key.startsWith(NO_HEADER)) return false;
      if (sha && /^[0-9a-f]{40}$/u.test(sha)) {
        const [said, section] = readFindings(body);
        return said.length === 0 && !section;
      }
      return at < newest;
    });
    if (failed) return false;
  }
  if (reads.length === 0) return required.size === 0;
  if ([...required].some((key) => !(key in entries))) return false;
  const shas = new Set(reads.map(([, sha]) => sha));
  const [sha] = shas;
  return shas.size === 1 && typeof sha === "string" && (sha === head || sameChange(sha as string));
}

/** A time after every vector post's: the reads a carried-out plan adds come last. */
const LATER = "2026-12-31T00:00:00Z";

const lensesOf = (plan: ReturnType<typeof planLensStart>) => plan.starts.map((s) => s.lens);

describe("lens_start on the shared vectors", () => {
  it("every case: a first read starts each lens the gate requires, one child each, nothing more", () => {
    let ran = 0;
    for (const c of V.cases) {
      const review = configs[c.config]!;
      // Shadow: the gate requires only its input; T3 adds the path lenses (the trial shadow is for).
      const shadow = review.mode === "shadow";
      const plan = planLensStart(
        input({
          review,
          required: shadow ? ["code"] : [],
          pullList: [pull(7, { changes: c.changes })],
        }),
      );
      assert.deepStrictEqual(plan.refused, [], c.name);
      const want = shadow && !c.lenses.includes("code") ? [...c.lenses, "code"] : c.lenses;
      assert.deepStrictEqual(lensesOf(plan), want, c.name);
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

  it("every shared vector state: after the plan is carried out the gate passes, or a person must act", () => {
    // States: each scan's posts, each round's history as reports, and mixed heads the vectors do not hold.
    const states: Array<[string, LensStartPost[]]> = [
      ...V.scans.map((v: { name: string; posts: VectorPost[] }) => [
        `scan ${v.name}`,
        vectorPosts(v.posts),
      ]),
      ...V.rounds.map((r: { name: string; history: Array<[string, string | null]> }) => [
        `round ${r.name}`,
        r.history.flatMap(([name, sha]) => (sha ? [report(name, sha)] : [])),
      ]),
      ["two lenses, one head each", [report("data-1", A), report("code", B)]],
      ["a lens no longer required", [report("data-1", A), report("data-2", A)]],
      ["an override covers an older read", [report("code", B), override(A)]],
      [
        "an override covers a comment that reads nothing",
        [{ ...report("code", B), body: `Review done.\n${report("code", B).body}` }, override(A)],
      ],
      ["a read newer than an override", [override(A), report("code", B)]],
      // The gate (enforce) cannot read the findings of an edited report that only a non-person override covers.
      [
        "an override covers an edited report",
        [{ ...report("code", A), edited: true }, override(A)],
      ],
      ["an override covers a short read", [report("code", A.slice(0, 7)), override(A)]],
    ];
    // How a read relates to the head: the commit itself only; the same own diff but another commit (an amended
    // message, a rebase, a force-push: the gate fails it); the same diff and the head merges it with the base.
    const ways: Array<
      [string, ((sha: string) => string) | undefined, ((sha: string) => boolean) | undefined]
    > = [
      ["own", undefined, undefined],
      ["amended", () => "one change", undefined],
      ["merged", () => "one change", () => true],
    ];
    let plans = 0;
    let carried = 0;
    let mixed = 0;
    let refusedByPerson = 0;
    for (const c of V.cases) {
      const review = configs[c.config]!;
      const enforce = review.mode === "enforce";
      for (const required of enforce ? [[], ["code"]] : [["code"], ["code", "data-1"]]) {
        for (const [name, posts] of states) {
          for (const head of [A, F]) {
            for (const [way, changeOf, sameChange] of ways) {
              const at = `${c.name} / ${name} / ${required.join(",")} / ${head.slice(0, 1)} / ${way}`;
              const one = (list: LensStartPost[]) =>
                planLensStart(
                  input({
                    review,
                    required,
                    pullList: [
                      pull(7, {
                        head,
                        posts: list,
                        changes: c.changes,
                        ...(changeOf ? { changeOf } : {}),
                        ...(sameChange ? { sameChange } : {}),
                      }),
                    ],
                  }),
                );
              const plan = one(posts);
              plans++;
              if (plan.refused.length > 0) {
                assert.lengthOf(plan.refused, 1, at);
                assert.include(plan.refused[0]!.reason, "a person", at);
                assert.deepStrictEqual(plan.starts, [], at);
                refusedByPerson++;
                continue;
              }
              const after = [
                ...posts,
                ...plan.starts.flatMap((s) =>
                  s.prs.map((r) => ({ ...report(s.lens, r.head), when: LATER })),
                ),
              ];
              if (plan.starts.length > 0) carried++;
              if (
                new Set([...gateReads(posts, V.override_prefix).values()].map((r) => r.sha)).size >
                1
              )
                mixed++;
              assert.isTrue(
                gatePasses(review, required, c.lenses, after, head, changeOf, sameChange),
                at,
              );
              // Runs again: the carried-out plan starts nothing more.
              assert.deepStrictEqual(one(after).starts, [], `${at}: again`);
            }
          }
        }
      }
    }
    assert.isAtLeast(plans, V.cases.length * (V.scans.length + V.rounds.length) * 12);
    assert.isAtLeast(carried, 1000);
    assert.isAtLeast(mixed, 100);
    assert.isAtLeast(refusedByPerson, 100);
  });
});

describe("lens_start reads the posts as the gate does (the shared `reads` vectors)", () => {
  it("every reads vector: each lens's read, the lenses that must post again, and whether the gate passes", () => {
    assert.isAtLeast(V.reads.length, 10);
    for (const v of V.reads) {
      const posts = vectorPosts(v.posts as VectorPost[], V.override_prefix);
      const review: ReviewBlock = {
        ...basic,
        mode: "enforce",
        findings: true,
        override_authors: v.override_authors,
      };
      const sameChange = (read: string) => (v.same as string[]).includes(read);
      const reads = gateReads(posts, V.override_prefix);
      const again = postAgainKeys(posts, review, v.head, sameChange, V.override_prefix);
      assert.deepStrictEqual(
        {
          reads: Object.fromEntries([...reads].map(([key, r]) => [key, r.sha])),
          again: [...again.keys()].toSorted(byCodePoint),
          passes:
            again.size === 0 &&
            gateReadsPass(reads, (v.required as string[]).map(lensKey), v.head, sameChange),
        },
        v.expect,
        v.name,
      );
    }
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

  it("the newest post by time counts, not the last one listed (the gate lists reviews after comments)", () => {
    const t = (second: number) => `2026-01-01T00:00:0${second}Z`;
    const plan = (posts: LensStartPost[]) =>
      lensesOf(planLensStart(input({ pullList: [pull(1, { posts })] })));
    // The comment reading the head is newer than the review listed after it.
    assert.deepStrictEqual(
      plan([
        { ...report("code", A), when: t(9) },
        { ...report("code", B), when: t(1) },
      ]),
      [],
    );
    assert.deepStrictEqual(
      plan([
        { ...report("code", A), when: t(1) },
        { ...report("code", B), when: t(9) },
      ]),
      ["code"],
    );
    // An override listed last but older than the read covers nothing.
    const late = [
      { ...report("code", B), when: t(9) },
      { ...override(A), when: t(5) },
    ];
    assert.strictEqual(gateReads(late, V.override_prefix).get("code")?.sha, B);
    assert.deepStrictEqual(plan(late), ["code"]);
    // Of two overrides the newest by time covers, not the last listed (an override posted as a review is listed last).
    const two = [
      { ...report("code", C), when: t(1) },
      { ...override(A), when: t(5) },
      { ...override(B), when: t(3) },
    ];
    assert.strictEqual(gateReads(two, V.override_prefix).get("code")?.sha, A);
    assert.deepStrictEqual(
      lensesOf(
        planLensStart(
          input({ review: null, required: ["code"], pullList: [pull(1, { posts: two })] }),
        ),
      ),
      [],
    );
    // Nor does it make a newer short read UNREAD in enforce.
    const short = [
      { ...report("code", A.slice(0, 7)), when: t(5) },
      { ...override(B), when: t(1) },
    ];
    const again = (posts: LensStartPost[]) => [
      ...postAgainKeys(posts, { ...basic, findings: true }, A).keys(),
    ];
    assert.deepStrictEqual(again(short), []);
    assert.deepStrictEqual(again(short.toReversed()), []);
    assert.deepStrictEqual(again([short[0]!, { ...short[1]!, when: t(9) }]), ["code"]);
  });

  it("a comment the gate's Unicode reads otherwise refuses that pull request only, naming the post and the way out", () => {
    // U+1C89 is unassigned in the gate's Python (Unicode 15.0) and a letter in this runtime's.
    const drift = { ...report("code", B), body: `${report("code", B).body}\n\u1c89major: x` };
    const plan = planLensStart(
      input({
        pullList: [pull(1, { posts: [drift] }), pull(2)],
      }),
    );
    assert.deepStrictEqual(plan.refused, [
      {
        pr: 1,
        reason:
          `its lens comments cannot be read as the gate reads them: the post report code ${B} holds U+1C89, ` +
          "which the gate's Python (Unicode 15.0.0) and this runtime assign differently, so the two would read it " +
          "differently. The gate reads that post; lens_start does not, so it starts nothing here until someone " +
          "edits the post to drop U+1C89 or deletes it (hiding it is not enough), then calls lens_start again",
      },
    ]);
    // A symbol new after Unicode 15.0 (U+1FAE9, an emoji) is read alike by both, as no letter, digit or space: in a
    // chat comment or a report it refuses nothing.
    const chat = { ...report("x", A), body: "thanks \u{1fae9}", where: "chat" };
    const emoji = { ...report("code", A), body: `${report("code", A).body} \u{1fae9}` };
    for (const posts of [[chat], [emoji]]) {
      const read = planLensStart(input({ pullList: [pull(1, { posts })] }));
      assert.deepStrictEqual(read.refused, []);
    }
    assert.deepStrictEqual(
      plan.starts.map((s) => [s.lens, s.prs.map((p) => p.number)]),
      [["code", [2]]],
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

  it("a read before a clean base merge still read the head: the gate's same-change check says so", () => {
    const plan = planLensStart(
      input({
        pullList: [
          pull(1, {
            head: B,
            posts: [report("code", A)],
            changeOf: () => "same",
            sameChange: (read) => read === A,
          }),
        ],
      }),
    );
    assert.deepStrictEqual(lensesOf(plan), []);
    assert.deepStrictEqual(plan.alreadyRead, [{ pr: 1, lens: "code" }]);
  });

  it("the same diff in other commits (an amend, a rebase, a force-push) is not read: every lens starts again", () => {
    // The own diff matches, so it is no new round, but the gate fails a head that is not the read commit merged
    // with the base: lens_start must start the lens, never count it as read.
    for (const over of [{}, { sameChange: () => false }]) {
      const plan = planLensStart(
        input({
          pullList: [
            pull(1, {
              head: B,
              posts: [report("code", A)],
              changeOf: () => "same",
              ...over,
            }),
          ],
        }),
      );
      assert.deepStrictEqual(lensesOf(plan), ["code"]);
      assert.deepStrictEqual(plan.alreadyRead, []);
      assert.deepStrictEqual(plan.starts[0]!.prs, [{ number: 1, head: B, round: 1 }]);
    }
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
    assert.deepStrictEqual(
      moved.refused.filter((r) => r.pr === null),
      [
        {
          pr: null,
          reason:
            "lens code for #1 #2 is not started: a pull request of its batch moved; call lens_start again",
        },
      ],
    );
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
        `at most ${LENS_START_MAX_PRS} pull requests per call, not ${LENS_START_MAX_PRS + 1}`,
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
    // Exactly the cap is one call.
    const ten = Array.from({ length: LENS_START_MAX_PRS }, (_, i) => pull(i + 1));
    const full = planLensStart(input({ pullList: ten }));
    assert.deepStrictEqual(full.refused, []);
    assert.deepStrictEqual(
      full.starts.flatMap((s) => s.prs.map((r) => r.number)),
      ten.map((p) => p.number),
    );
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

  it("a repo without a review block: the gate's required-lenses input names the lenses, critical paths not guessed", () => {
    const named = ["code", "security"];
    const plan = planLensStart(
      input({
        review: null,
        required: named,
        pullList: [pull(1, { changes: [["M", ".github/workflows/ci.yml"]] })],
      }),
    );
    assert.deepStrictEqual(plan.refused, []);
    assert.deepStrictEqual(lensesOf(plan), named);
    assert.include(plan.notes[0], "no `review` block");
    assert.include(plan.notes[0], "required-lenses input (code, security)");
    assert.include(plan.starts[0]!.prompt, "correctness");
    const late = planLensStart(
      input({
        review: null,
        required: named,
        pullList: [
          pull(1, {
            head: C,
            posts: [
              report("code", A),
              report("security", A),
              report("code", B),
              report("security", B),
            ],
          }),
        ],
      }),
    );
    assert.deepStrictEqual(lensesOf(late), [...named, FALLBACK_REVIEW.rounds.approach_lens]);
    assert.include(late.starts[2]!.prompt, "approach can reach a mergeable state");
    const over = [A, B, C, D].map((sha) => report("code", sha));
    assert.include(
      planLensStart(
        input({ review: null, required: ["code"], pullList: [pull(1, { head: E, posts: over })] }),
      ).refused[0]!.reason,
      "past the cap of 4",
    );
    // Old or empty state: no block and no input, the gate requires no lens; lens_start starts none and says why.
    for (const required of [[], ["", "  "]]) {
      const none = planLensStart(input({ review: null, required }));
      assert.deepStrictEqual(none.starts, []);
      assert.deepStrictEqual(none.refused, [
        {
          pr: 1,
          reason:
            "the gate's required-lenses input names no lens and the config has no `review` block in mode " +
            "enforce, so the gate requires no lens here (it fails a head only when the lens comments there do " +
            "not all read it); lens_start starts only lenses the gate requires, so none: a person names the " +
            "lenses in the caller workflow or the config",
        },
      ]);
    }
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
    // Until lens_report (T4) exists, the child posts one new comment whose first line is the lens header.
    assert.include(prompt, `\`gh pr comment 4 --repo ${REPO} --body-file <file>\``);
    assert.include(
      prompt,
      `\`lens: code · reviewer: <your model> · read at: <the full head sha>\``,
    );
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

describe("lens_start plans what the gate reads (fix round A+B)", () => {
  const storage = [["M", "src/storage/x.sql"]];
  const carryOut = (posts: ReadonlyArray<LensStartPost>, plan: LensStartPlan) => [
    ...posts,
    ...plan.starts.flatMap((s) => s.prs.map((r) => report(s.lens, r.head))),
  ];

  it("a base merge kept data-1's read: every lens reads the new head, not data-2 alone", () => {
    // data-1 read A; the base merged in, head B is the same change, and now touches storage.
    const posts = [report("data-1", A)];
    const same = () => "one change";
    const plan = planLensStart(
      input({ pullList: [pull(1, { head: B, posts, changes: storage, changeOf: same })] }),
    );
    assert.deepStrictEqual(lensesOf(plan), ["data-1", "data-2"]);
    assert.deepStrictEqual(plan.alreadyRead, []);
    assert.isTrue(gatePasses(basic, [], ["data-1", "data-2"], carryOut(posts, plan), B, same));
    // Stopped half-way: data-2 reported B, data-1 did not; again, only data-1 starts.
    const half = [...posts, report("data-2", B)];
    const again = planLensStart(
      input({ pullList: [pull(1, { head: B, posts: half, changes: storage, changeOf: same })] }),
    );
    assert.deepStrictEqual(lensesOf(again), ["data-1"]);
    assert.isTrue(gatePasses(basic, [], ["data-1", "data-2"], carryOut(half, again), B, same));
  });

  it("a lens no longer required still names an old head: it reads the new head too", () => {
    const posts = [report("data-1", A), report("data-2", A)];
    const plan = planLensStart(input({ pullList: [pull(1, { head: B, posts })] }));
    assert.deepStrictEqual(lensesOf(plan), ["code", "data-1", "data-2"]);
    assert.isTrue(plan.notes.some((n) => n.includes("data-1 is no longer required but read " + A)));
    assert.isTrue(gatePasses(basic, [], ["code"], carryOut(posts, plan), B));
    // Runs again after it all reported: nothing more.
    assert.deepStrictEqual(
      lensesOf(
        planLensStart(input({ pullList: [pull(1, { head: B, posts: carryOut(posts, plan) })] })),
      ),
      [],
    );
  });

  it("an entry lens_start cannot start again: refused, and a person is told the way out", () => {
    const noHeader = { ...report("code", A), body: `Looks fine.\n${report("code", A).body}` };
    const stuck = planLensStart(input({ pullList: [pull(1, { posts: [noHeader] })] }));
    assert.deepStrictEqual(stuck.starts, []);
    assert.include(stuck.refused[0]!.reason, "a comment that reads nothing");
    assert.include(stuck.refused[0]!.reason, "a person writes a newer override comment");
    const unknown = planLensStart(input({ pullList: [pull(1, { posts: [report("perf", B)] })] }));
    assert.deepStrictEqual(unknown.starts, []);
    assert.include(unknown.refused[0]!.reason, `lens perf read ${B}`);
    // A person's newer override for the head covers the comment: the required lens still starts.
    const covered = [noHeader, override(A)];
    const after = planLensStart(input({ pullList: [pull(1, { posts: covered })] }));
    assert.deepStrictEqual(lensesOf(after), ["code"]);
    assert.isTrue(gatePasses(basic, [], ["code"], carryOut(covered, after), A));
  });

  it("a look-alike without the lens marker (#865): ignored and said so, or, with an open finding, held with the fix", () => {
    const note: LensStartPost = {
      ...report("code", A),
      body: "## Code lens\nAll fine.",
      where: "note",
    };
    const plan = planLensStart(input({ pullList: [pull(1, { posts: [note] })] }));
    assert.deepStrictEqual(lensesOf(plan), ["code"], "the lens has not reported: it starts");
    const said = plan.notes.filter((n) =>
      n.startsWith("#1: the comment note looks like a lens report"),
    );
    assert.lengthOf(said, 1);
    assert.include(said[0], "the gate ignores it");
    assert.include(said[0], NO_MARKER);
    assert.include(said[0], REPOST);
    assert.deepStrictEqual(planLensStart(input({ pullList: [pull(1, { posts: [note] })] })), plan);
    const twice = planLensStart(
      input({ pullList: [pull(1, { posts: [note, { ...note, where: "again" }] })] }),
    );
    assert.lengthOf(
      twice.notes.filter((n) => n.includes("the gate ignores it")),
      2,
    );
    const held: LensStartPost = {
      ...report("code", A),
      body: "## Code lens\nblocker: a re-run loses data\nmajor: none",
      where: "held",
    };
    for (const posts of [
      [report("code", A), held],
      [held, report("code", A)],
    ]) {
      const stuck = planLensStart(input({ pullList: [pull(1, { posts })] }));
      assert.deepStrictEqual(
        stuck.starts,
        [],
        "an older or newer clean read never lets it through",
      );
      assert.include(stuck.refused[0]!.reason, "a comment that reads nothing");
      assert.include(stuck.refused[0]!.reason, REPOST);
      assert.isFalse(gatePasses(basic, [], ["code"], posts, A));
    }
    const optedOut = { ...held, body: `not a lens: ${held.body}` };
    assert.isTrue(gatePasses(basic, [], ["code"], [report("code", A), optedOut], A));
  });

  it("a newer override the gate counts is honoured: nothing starts again", () => {
    const plan = planLensStart(
      input({ pullList: [pull(1, { head: B, posts: [report("code", A), override(B)] })] }),
    );
    assert.deepStrictEqual(lensesOf(plan), []);
    assert.deepStrictEqual(plan.alreadyRead, [{ pr: 1, lens: "code" }]);
    // An override of another head covers nothing for this head.
    const other = planLensStart(
      input({ pullList: [pull(1, { head: B, posts: [report("code", B), override(A)] })] }),
    );
    assert.deepStrictEqual(lensesOf(other), ["code"]);
  });

  it("the required-lenses input joins the plan; a name only it knows reads with the built-in prompt", () => {
    const plan = planLensStart(input({ required: ["Security", "code"] }));
    assert.deepStrictEqual(lensesOf(plan), ["code", "Security"]);
    assert.include(plan.starts[1]!.prompt, "correctness");
    assert.include(plan.notes[0], "names Security, which the config's lenses do not");
    // Shadow: the gate requires only the input; T3 still starts the path lenses.
    const shadow = configs["two-critical"]!;
    assert.strictEqual(shadow.mode, "shadow");
    const trial = planLensStart(
      input({ review: shadow, required: ["code"], pullList: [pull(1, { changes: storage })] }),
    );
    assert.deepStrictEqual(trial.refused, []);
    assert.include(lensesOf(trial), "code");
  });

  it("an input or mode the gate refuses: no lens is started, a person fixes it", () => {
    const shadow = configs["two-critical"]!;
    for (const [over, words] of [
      [{ review: shadow, required: [] }, "a person names the lenses"],
      [{ required: null }, "required-lenses input at main was not read"],
      [{ required: ["code", "<name>"] }, "a person fixes the caller workflow"],
      [{ required: ["r2"] }, "a person fixes the caller workflow"],
      [{ review: { ...basic, mode: undefined } }, "a person fixes the config"],
      [{ review: { ...basic, mode: "on" as never } }, "a person fixes the config"],
    ] as const) {
      const plan = planLensStart(input(over as never));
      assert.deepStrictEqual(plan.starts, [], words);
      assert.include(plan.refused[0]!.reason, words);
    }
  });

  it("each pull request of a batch is planned with its own base branch's rules", () => {
    const pulls = [
      pull(1),
      pull(2, { head: B, base: "release" }),
      pull(3, { head: C, base: "dev" }),
    ];
    const plan = planLensStart({
      ...input({ pullList: pulls }),
      bases: new Map([
        ["main", baseOf(basic)],
        ["release", baseOf(null, ["security"])],
      ]),
    });
    assert.deepStrictEqual(
      plan.starts.map((s) => [s.lens, s.prs.map((r) => r.number)]),
      [
        ["code", [1]],
        ["security", [2]],
      ],
    );
    assert.deepStrictEqual(plan.refused, [
      { pr: 3, reason: "its base branch dev was not read: no lens is started rather than too few" },
    ]);
  });

  it("a lens child that ended without its read starts again with the next attempt; a running one is returned", () => {
    const first = planLensStart(input({}));
    const [child] = first.starts;
    assert.strictEqual(child!.attempt, 1);
    assert.deepStrictEqual(child!.replaces, []);
    const earlier = (running: boolean, ...ids: string[]) =>
      ids.map((requestId) => ({ requestId, key: "code", prs: [{ number: 1, head: A }], running }));
    const second = planLensStart(input({ children: earlier(false, child!.requestId) }));
    assert.strictEqual(second.starts[0]!.attempt, 2);
    assert.strictEqual(second.starts[0]!.requestId, `${child!.requestId}/again-2`);
    assert.deepStrictEqual(second.starts[0]!.replaces, [child!.requestId]);
    assert.include(
      second.notes,
      `lens code for #1: 1 earlier child of it ended at this head with no read the gate counts (the last: ` +
        `${child!.requestId}), so it starts again as attempt 2`,
    );
    const third = planLensStart(
      input({ children: earlier(false, child!.requestId, second.starts[0]!.requestId) }),
    );
    assert.strictEqual(third.starts[0]!.attempt, 3);
    // Stops half-way again and again: after the cap of attempts a person looks, nothing restarts forever.
    const ids = [child!.requestId, second.starts[0]!.requestId, third.starts[0]!.requestId];
    assert.strictEqual(LENS_START_MAX_ATTEMPTS, 3);
    const capped = planLensStart(input({ children: earlier(false, ...ids) }));
    assert.deepStrictEqual(capped.starts, []);
    assert.deepStrictEqual(capped.refused, [
      {
        pr: 1,
        reason:
          "lens code is not started: 3 of its children ended with no read the gate counts at this head (no " +
          `report, or one edited, hidden or unread since; the last: ${ids[2]}); a person looks at why before it ` +
          "starts again, and a new head starts it afresh",
      },
    ]);
    // A second call while the child still runs: that child, never a duplicate.
    const busy = planLensStart(input({ children: earlier(true, child!.requestId) }));
    assert.deepStrictEqual(busy.starts, []);
    assert.deepStrictEqual(busy.running, [
      { requestId: child!.requestId, key: "code", prs: [{ number: 1, head: A }] },
    ]);
    // A batch call overlapping it: only the other pull request gets a child.
    const batch = planLensStart(
      input({
        pullList: [pull(1), pull(2, { head: B })],
        children: earlier(true, child!.requestId),
      }),
    );
    assert.deepStrictEqual(
      batch.starts.map((s) => s.prs.map((r) => r.number)),
      [[2]],
    );
    assert.lengthOf(batch.running, 1);
    // A child of an old head is no child of this one.
    const moved = planLensStart(
      input({ pullList: [pull(1, { head: B })], children: earlier(true, child!.requestId) }),
    );
    assert.deepStrictEqual(lensesOf(moved), ["code"]);
  });

  it("the attempt cap counts per lens, pull request and head: no other batch starts a capped lens again", () => {
    const dead = (reads: Array<[number, string]>, attempt: number) => {
      const prs = reads.map(([number, head]) => ({ number, head }));
      return {
        requestId: lensRequestId(REPO, "code", prs, attempt),
        key: "code",
        prs,
        running: false,
      };
    };
    const shape = (plan: LensStartPlan) => ({
      starts: plan.starts.map((s) => [s.prs.map((r) => r.number), s.attempt]),
      refused: plan.refused.map((r) => r.pr),
    });
    const alone = [1, 2, 3].map((n) => dead([[1, A]], n));
    // #1 capped alone, then named with #2: #1 is refused, #2 starts on its own.
    const withTwo = planLensStart(
      input({ pullList: [pull(1), pull(2, { head: B })], children: alone }),
    );
    assert.deepStrictEqual(shape(withTwo), { starts: [[[2], 1]], refused: [1] });
    // #1 capped in [#1, #2] batches, then named with #3.
    const pair = [1, 2, 3].map((n) =>
      dead(
        [
          [1, A],
          [2, B],
        ],
        n,
      ),
    );
    const withThree = planLensStart(
      input({ pullList: [pull(1), pull(3, { head: C })], children: pair }),
    );
    assert.deepStrictEqual(shape(withThree), { starts: [[[3], 1]], refused: [1] });
    // The batch shrinks after a partial post: #2 was read, so #1 is planned alone, and it is capped all the same.
    const shrunk = planLensStart(
      input({
        pullList: [pull(1), pull(2, { head: B, posts: [report("code", B)] })],
        children: pair,
      }),
    );
    assert.deepStrictEqual(shape(shrunk), { starts: [], refused: [1] });
    assert.deepStrictEqual(shrunk.alreadyRead, [{ pr: 2, lens: "code" }]);
    // One dead batch child counts for each of its pull requests: #1 alone is attempt 2 and replaces it.
    const once = planLensStart(input({ children: [pair[0]!] }));
    assert.deepStrictEqual(shape(once), { starts: [[[1], 2]], refused: [] });
    assert.deepStrictEqual(once.starts[0]!.replaces, [pair[0]!.requestId]);
    assert.strictEqual(
      once.starts[0]!.requestId,
      lensRequestId(REPO, "code", [{ number: 1, head: A }], 2),
    );
    // A new head starts afresh; another lens has its own count.
    assert.deepStrictEqual(
      shape(planLensStart(input({ pullList: [pull(1, { head: D })], children: alone }))),
      {
        starts: [[[1], 1]],
        refused: [],
      },
    );
    // A lost record (attempt 1's) never plans an id that already ended: attempts 2 and 3 are known, so 4 starts.
    const lost = planLensStart(input({ children: alone.slice(1) }));
    assert.deepStrictEqual(shape(lost), { starts: [[[1], 4]], refused: [] });
    assert.notInclude(
      alone.map((c) => c.requestId),
      lost.starts[0]!.requestId,
    );
  });

  it("the cap counts only this lens's children, and a batch starts at its largest count", () => {
    const dead = (key: string, reads: Array<[number, string]>, attempt: number) => {
      const prs = reads.map(([number, head]) => ({ number, head }));
      return { requestId: lensRequestId(REPO, key, prs, attempt), key, prs, running: false };
    };
    const shape = (plan: LensStartPlan) => ({
      starts: plan.starts.map((s) => [s.lens, s.prs.map((r) => r.number), s.attempt]),
      refused: plan.refused.map((r) => r.pr),
    });
    // Three ended children of another lens at this very head: lens code has its own count, so it starts as attempt 1.
    const other = [1, 2, 3].map((n) => dead("data", [[1, A]], n));
    assert.deepStrictEqual(shape(planLensStart(input({ children: other }))), {
      starts: [["code", [1], 1]],
      refused: [],
    });
    // #1 had two children end, #2 none: the batch is attempt 3, never 1, and replaces both.
    const two = [1, 2].map((n) => dead("code", [[1, A]], n));
    const batch = planLensStart(
      input({ pullList: [pull(1), pull(2, { head: B })], children: two }),
    );
    assert.deepStrictEqual(shape(batch), { starts: [["code", [1, 2], 3]], refused: [] });
    assert.deepStrictEqual(
      batch.starts[0]!.replaces,
      two.map((c) => c.requestId),
    );
    assert.include(
      batch.notes,
      "lens code for #1 #2: 2 earlier children of it ended at one of these heads with no read the gate counts " +
        `(the last: ${two[1]!.requestId}), so it starts again as attempt 3`,
    );
  });

  it("a head read again after a force-push back to it: the read a newer post replaced is no failed attempt", () => {
    const dead = (attempt: number) => ({
      requestId: lensRequestId(REPO, "code", [{ number: 1, head: A }], attempt),
      key: "code",
      prs: [{ number: 1, head: A }],
      running: false,
    });
    const shape = (plan: LensStartPlan) => ({
      starts: plan.starts.map((s) => [s.prs.map((r) => r.number), s.attempt]),
      refused: plan.refused.map((r) => r.reason),
    });
    // Head A, read; B, read; A again (force-push). Of three children at A, one read it: two failed, under the cap.
    const ended = [1, 2, 3].map(dead);
    const back = [report("code", A), report("code", B)];
    const plan = planLensStart(input({ pullList: [pull(1, { posts: back })], children: ended }));
    assert.deepStrictEqual(shape(plan), { starts: [[[1], 4]], refused: [] });
    assert.include(
      plan.notes,
      "lens code for #1: 3 earlier children of it ended at this head (2 with no read the gate counts, 1 whose read " +
        `a newer post of the lens replaced) (the last: ${ended[2]!.requestId}), so it starts again as attempt 4`,
    );
    // A read of A that did not count (edited, or no findings section in enforce) is a failed attempt: capped.
    const edited = { ...report("code", A), edited: true };
    const bare = {
      ...report("code", A),
      body: `lens: code · reviewer: x · read at: ${A}\nstate: fine`,
    };
    for (const first of [edited, bare]) {
      const capped = planLensStart(
        input({ pullList: [pull(1, { posts: [first, report("code", B)] })], children: ended }),
      );
      assert.deepStrictEqual(capped.starts, [], first.body);
      assert.include(
        capped.refused[0]!.reason,
        "3 of its children ended with no read the gate counts",
        first.body,
      );
    }
    // A child that posts a read and then replaces it, again and again: twice the cap stops it all the same.
    const many = [1, 2, 3, 4, 5, 6].map(dead);
    const posts = many.flatMap(() => [report("code", A), report("code", B)]);
    const stopped = planLensStart(input({ pullList: [pull(1, { posts })], children: many }));
    assert.deepStrictEqual(shape(stopped), {
      starts: [],
      refused: [
        "lens code is not started: 6 of its children ended at this head, and a newer post of the lens replaced " +
          `each read the gate counted (the last: ${many[5]!.requestId}); a person looks at why before it starts ` +
          "again, and a new head starts it afresh",
      ],
    });
  });

  it("the caller stores each child's lens and pull requests: a hashed long id is still found again", () => {
    const long = `code-${"x".repeat(240)}`;
    const first = planLensStart(input({ review: null, required: [long] }));
    const [child] = first.starts;
    assert.match(child!.requestId, /^lens\/[0-9a-f]{64}$/u);
    assert.deepStrictEqual(earlierChild(child!, true), {
      requestId: child!.requestId,
      key: lensKey(long),
      prs: [{ number: 1, head: A }],
      running: true,
    });
    const busy = planLensStart(
      input({ review: null, required: [long], children: [earlierChild(child!, true)] }),
    );
    assert.deepStrictEqual(busy.starts, []);
    assert.lengthOf(busy.running, 1);
    const again = planLensStart(
      input({ review: null, required: [long], children: [earlierChild(child!, false)] }),
    );
    assert.strictEqual(again.starts[0]!.attempt, 2);
    assert.deepStrictEqual(again.starts[0]!.replaces, [child!.requestId]);
  });

  it("past the round cap a head the gate already passes needs no person, unless the gate judges rounds", () => {
    // Round 5 by its marker, and every lens already read this head.
    const posts = [report("code r5", A)];
    const fallback = planLensStart(
      input({ review: null, required: ["code"], pullList: [pull(1, { posts })] }),
    );
    assert.deepStrictEqual(fallback.refused, []);
    assert.deepStrictEqual(fallback.starts, []);
    assert.deepStrictEqual(fallback.alreadyRead, [{ pr: 1, lens: "code" }]);
    assert.deepStrictEqual(fallback.notes, [
      "#1: a read of this head would be review round 5, past T3's cap of 4; the gate already passes it and " +
        "judges no rounds here, so nothing starts",
    ]);
    const shadow = planLensStart(
      input({
        review: configs["two-critical"]!,
        required: ["code"],
        pullList: [pull(1, { posts })],
      }),
    );
    assert.deepStrictEqual(shadow.refused, []);
    assert.deepStrictEqual(shadow.starts, []);
    // In enforce the gate fails the head past the cap: a person decides.
    const enforce = planLensStart(input({ pullList: [pull(1, { posts })] }));
    assert.deepStrictEqual(enforce.starts, []);
    assert.include(enforce.refused[0]!.reason, "review round 5, past the cap of 4");
    // Past the cap and not yet passing: refused everywhere.
    const fresh = planLensStart(
      input({ review: null, required: ["code"], pullList: [pull(1, { head: B, posts })] }),
    );
    assert.include(fresh.refused[0]!.reason, "past the cap of 4");
  });

  it("enforce: a report that reads nothing and only a non-person override covers is read again", () => {
    const edited = { ...report("code", A), edited: true };
    for (const posts of [
      [edited, override(A)],
      [report("code", A.slice(0, 7)), override(A)],
    ]) {
      const plan = planLensStart(input({ pullList: [pull(1, { posts })] }));
      assert.deepStrictEqual(plan.refused, []);
      assert.deepStrictEqual(lensesOf(plan), ["code"]);
      assert.deepStrictEqual(plan.alreadyRead, []);
      // Shadow judges no findings: the override's read stands there.
      const shadow = planLensStart(
        input({
          review: configs["two-critical"]!,
          required: ["code"],
          pullList: [pull(1, { posts })],
        }),
      );
      assert.deepStrictEqual(shadow.starts, []);
    }
    // A person's override for this head clears it: nothing starts.
    const people = { ...basic, override_authors: ["Someone"] };
    const person = planLensStart(
      input({
        review: people,
        pullList: [pull(1, { posts: [edited, { ...override(A), author: "someone" }] })],
      }),
    );
    assert.deepStrictEqual(person.starts, []);
    assert.deepStrictEqual(person.alreadyRead, [{ pr: 1, lens: "code" }]);
    // A listed login's override for another change clears nothing: the agent's newer override still leaves it UNREAD.
    const other = planLensStart(
      input({
        review: people,
        pullList: [
          pull(1, {
            head: B,
            posts: [
              { ...report("code", B), edited: true },
              { ...override(A), author: "someone" },
              override(B),
            ],
          }),
        ],
      }),
    );
    assert.deepStrictEqual(lensesOf(other), ["code"]);
    // One for the same change (the head is it merged with the base) clears it like one for the head.
    const same = planLensStart(
      input({
        review: people,
        pullList: [
          pull(1, {
            head: B,
            sameChange: (read) => read === A,
            posts: [
              { ...report("code", B), edited: true },
              { ...override(A), author: "someone" },
            ],
          }),
        ],
      }),
    );
    assert.deepStrictEqual(lensesOf(same), []);
    // Findings not judged (findings: false): the gate fails no UNREAD entry, so the override's read stands.
    const unjudged = planLensStart(
      input({
        review: { ...basic, findings: false },
        pullList: [pull(1, { posts: [edited, override(A)] })],
      }),
    );
    assert.deepStrictEqual(unjudged.starts, []);
    assert.deepStrictEqual(unjudged.alreadyRead, [{ pr: 1, lens: "code" }]);
  });

  it("enforce: a report of the head with no findings section is read again; the gate fails it until it posts again", () => {
    const bare = {
      ...report("code", A),
      body: `lens: code · reviewer: x · read at: ${A}\nstate: fine`,
    };
    const plan = planLensStart(input({ pullList: [pull(1, { posts: [bare] })] }));
    assert.deepStrictEqual(lensesOf(plan), ["code"]);
    assert.deepStrictEqual(plan.alreadyRead, []);
    assert.include(
      plan.notes,
      "#1: lens code has a newest report with no findings section, so the gate fails this head until it posts " +
        "again: it reads the head again",
    );
    // Not in shadow or with findings off (the gate judges no findings there), and not once a person cleared it.
    for (const review of [
      { ...basic, findings: false },
      { ...basic, mode: "shadow" as const },
    ]) {
      const off = planLensStart(
        input({ review, required: ["code"], pullList: [pull(1, { posts: [bare] })] }),
      );
      assert.deepStrictEqual(off.starts, [], review.mode);
    }
    const cleared = planLensStart(
      input({
        review: { ...basic, override_authors: ["someone"] },
        pullList: [pull(1, { posts: [bare, { ...override(A), author: "someone" }] })],
      }),
    );
    assert.deepStrictEqual(cleared.starts, []);
  });

  it("shadow says it starts the path lenses the gate does not require, on purpose", () => {
    const plan = planLensStart(
      input({
        review: configs["two-critical"]!,
        required: ["code"],
        pullList: [
          pull(1, {
            changes: V.cases.find(
              (c: { config: string; lenses: string[] }) =>
                c.config === "two-critical" && c.lenses.some((l) => l !== "code"),
            )!.changes,
          }),
        ],
      }),
    );
    const extra = lensesOf(plan).filter((l) => l !== "code");
    assert.isAbove(extra.length, 0);
    assert.deepStrictEqual(plan.notes, [
      `#1: in shadow the gate requires only its required-lenses input; lens_start also starts ${extra.join(", ")} ` +
        "of the path rules on purpose (the trial shadow is for); each costs a child slot and its runner's usage, " +
        "Fable usage for a lens that runs on Fable",
    ]);
  });

  it("heads and repo in any case: one pull request, one id", () => {
    const twice = planLensStart({
      ...input({}),
      prs: [
        { number: 1, head: A },
        { number: 1, head: A.toUpperCase() },
      ],
    });
    assert.deepStrictEqual(twice.refused, []);
    assert.lengthOf(twice.starts, 1);
    assert.strictEqual(
      planLensStart({ ...input({}), repo: "Owner/Repo" }).starts[0]!.requestId,
      planLensStart(input({})).starts[0]!.requestId,
    );
    const upper = planLensStart(
      input({ pullList: [pull(1, { head: A.toUpperCase(), posts: [report("code", A)] })] }),
    );
    assert.deepStrictEqual(lensesOf(upper), []);
  });

  it("the approach lens runs on the approach runner by its role, whatever the config names it", () => {
    const own: ReviewBlock = {
      ...basic,
      lenses: { ...basic.lenses, "stuck-audit": { prompt: "stuck-audit.md" } },
      rounds: { ...basic.rounds, approach_lens: "stuck-audit" },
    };
    const posts = [report("code", A), report("code", B)];
    const plan = planLensStart(input({ review: own, pullList: [pull(1, { head: C, posts })] }));
    assert.deepStrictEqual(lensesOf(plan), ["code", "stuck-audit"]);
    assert.strictEqual(plan.starts[1]!.runner.model, "claude-fable-5-2");
    const rules = effectiveReviewRules({
      preset: "recommended",
      lensRunners: { "stuck-audit": { driver: ON.runner.driver, model: "m", effort: "high" } },
    });
    const set = planLensStart(
      input({ rules, review: own, pullList: [pull(1, { head: C, posts })] }),
    );
    assert.strictEqual(set.starts[1]!.runner.model, "m");
  });

  it("a person's override with a lens still missing says so", () => {
    const plan = planLensStart(input({ pullList: [pull(1, { decided: true, changes: storage })] }));
    assert.deepStrictEqual(plan.starts, []);
    assert.include(plan.refused[0]!.reason, "still misses lens data-1, data-2");
  });

  it("refusals: a lens with no entry, bad numbers, long and odd ids", () => {
    const { "data-2": _gone, ...lenses } = basic.lenses;
    const plan = planLensStart(
      input({ review: { ...basic, lenses }, pullList: [pull(1, { changes: storage })] }),
    );
    assert.include(plan.refused[0]!.reason, "data-2 (no entry in lenses)");
    for (const number of [1.5, Number.NaN, -1]) {
      assert.include(
        planLensStart({ ...input({}), prs: [{ number }] }).refused[0]!.reason,
        "is not a pull request number",
      );
    }
    const many = Array.from({ length: 10 }, (_, i) => ({ number: i + 1, head: A }));
    const long = lensRequestId(`o/${"r".repeat(240)}`, "code", many);
    assert.isAtMost(long.length, 256);
    assert.match(long, /^lens\/[0-9a-f]{64}$/u);
    assert.notStrictEqual(lensRequestId(REPO, "\ud800", many), lensRequestId(REPO, "\ud801", many));
    assert.notStrictEqual(lensRequestId(REPO, "\ud800", many), lensRequestId(REPO, "�", many));
  });
});
