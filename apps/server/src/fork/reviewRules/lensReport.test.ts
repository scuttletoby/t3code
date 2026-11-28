// @effect-diagnostics nodeBuiltinImport:off
/**
 * Fork: lens_report's rules (lensReport.ts). The shared `written` vectors hold the comment T3 posts for a report and
 * what the gate reads of it; review_judge.test.py replays the same comments through the gate.
 */
import * as NodeFS from "node:fs";
import { assert, describe, it } from "@effect/vitest";
import { effectiveReviewRules, type EffectiveReviewRules } from "@t3tools/contracts";

import { readFindings } from "./findings.ts";
import { lensKey } from "./lensComment.ts";
import {
  lensReportHeader,
  planLensReport,
  reviewerName,
  writeLensReport,
  type LensReportPlan,
} from "./lensReport.ts";
import { gateReads, gateReadsPass, postAgainKeys } from "./lensStart.ts";
import type { PullRead, RawPost, StoredChild } from "./lensStartRun.ts";
import type { ReviewBlock } from "./plan.ts";
import { byCodePoint } from "./pyText.ts";
import { vectorPosts } from "./vectors.testkit.ts";

const V = JSON.parse(
  NodeFS.readFileSync(
    new URL("../../../../../fork-ci/actions/pr-queue/review.vectors.json", import.meta.url),
    "utf8",
  ),
);
const basic = (V.configs as Record<string, ReviewBlock>).basic!;
const ON = effectiveReviewRules({ preset: "recommended" }) as Extract<
  EffectiveReviewRules,
  { readonly preset: "recommended" }
>;
const [A, B] = ["a", "b"].map((x) => x.repeat(40)) as [string, string];
const CLEAN = "state: fine\nblocker: none\nmajor: none";

interface Written {
  readonly name: string;
  readonly lens: string;
  readonly reviewer: string;
  readonly head: string;
  readonly body: string;
  readonly comment: string;
  readonly refused: string | null;
  readonly expect: {
    readonly reads: Record<string, string | null>;
    readonly again: ReadonlyArray<string>;
    readonly passes: boolean;
  };
}

describe("lens_report writes the comment the gate reads (the shared `written` vectors)", () => {
  it("every written vector: the comment T3 posts or why it refuses, and what the gate reads of it", () => {
    const written = V.written as ReadonlyArray<Written>;
    assert.isAtLeast(written.length, 10);
    const review: ReviewBlock = { ...basic, mode: "enforce", findings: true, override_authors: [] };
    for (const v of written) {
      const got = writeLensReport({
        lens: v.lens,
        reviewer: v.reviewer,
        head: v.head,
        body: v.body,
        maxChars: ON.reportMaxChars,
      });
      if (v.refused === null) {
        assert.deepStrictEqual(got, { type: "written", comment: v.comment }, v.name);
      } else {
        assert.strictEqual(got.type, "refused", v.name);
        assert.include(got.type === "refused" ? got.reason : "", v.refused, v.name);
      }
      const posts = vectorPosts([{ body: v.comment }], V.override_prefix);
      const reads = gateReads(posts, V.override_prefix);
      const again = postAgainKeys(posts, review, v.head, undefined, V.override_prefix);
      assert.deepStrictEqual(
        Object.fromEntries([...reads].map(([key, r]) => [key, r.sha])),
        v.expect.reads,
        `${v.name}: reads`,
      );
      assert.deepStrictEqual([...again.keys()].toSorted(byCodePoint), v.expect.again, v.name);
      // lens_start's copy of the verdict reads no finding: it is checked where the report holds none.
      if (readFindings(v.comment)[0].length === 0) {
        assert.strictEqual(
          again.size === 0 && gateReadsPass(reads, [lensKey(v.lens)], v.head),
          v.expect.passes,
          `${v.name}: passes`,
        );
      }
      if (v.refused === null) {
        assert.deepStrictEqual([v.expect.reads, v.expect.again], [{ [v.lens]: v.head }, []]);
      }
    }
  });

  it("a report longer than the rules allow, with its header, is refused; one at the limit is posted", () => {
    const header = lensReportHeader("code", "model-x", A);
    const body = `${CLEAN}\n${"x".repeat(100)}`;
    const length = header.length + 2 + body.length;
    const at = writeLensReport({
      lens: "code",
      reviewer: "model-x",
      head: A,
      body,
      maxChars: length,
    });
    assert.strictEqual(at.type, "written");
    const over = writeLensReport({
      lens: "code",
      reviewer: "model-x",
      head: A,
      body,
      maxChars: length - 1,
    });
    assert.deepStrictEqual(over, {
      type: "refused",
      reason: `the report is ${length} characters with its first line, more than the ${length - 1} the review rules allow: shorten it`,
    });
  });

  it("a letter the gate's Python does not know is refused, not posted to be read otherwise", () => {
    const got = writeLensReport({
      lens: "code",
      reviewer: "model-x",
      head: A,
      body: "blocker: none\nᲉmajor: x",
      maxChars: ON.reportMaxChars,
    });
    assert.strictEqual(got.type, "refused");
    assert.include(got.type === "refused" ? got.reason : "", "U+1C89");
  });

  it("the reviewer is one short line, and never empty", () => {
    assert.strictEqual(reviewerName("  a\n b\t"), "a b");
    assert.strictEqual(reviewerName(" \n "), "unknown");
    assert.strictEqual(reviewerName("m".repeat(200)).length, 80);
  });
});

const child = (over: Partial<StoredChild> = {}): StoredChild => ({
  requestId: "lens/owner/repo#7/aaaaaaa/code",
  key: "code",
  prs: [{ number: 7, head: A }],
  taskId: "task-1",
  childThreadId: "thread-child",
  parentThreadId: "thread-orchestrator",
  startedAt: "2026-01-01T00:00:00Z",
  ...over,
});

const post = (body: string, over: Partial<RawPost> = {}): RawPost => ({
  body,
  when: "2026-01-01T00:00:01Z",
  url: "https://example.test/pull/7#c1",
  login: "someone",
  bot: false,
  association: "OWNER",
  edited: false,
  hidden: false,
  ...over,
});

const pull = (over: Partial<PullRead> = {}): PullRead => ({
  number: 7,
  state: "open",
  head: A,
  base: "main",
  headBranch: "feat/x",
  posts: [],
  ...over,
});

const plan = (over: Partial<Parameters<typeof planLensReport>[0]> = {}): LensReportPlan =>
  planLensReport({
    rules: ON,
    caller: "thread-child",
    reviewer: "model-x",
    pr: 7,
    body: CLEAN,
    children: [child()],
    pull: pull(),
    ...over,
  });

const COMMENT = `lens: code · reviewer: model-x · read at: ${A}\n\n${CLEAN}`;

describe("lens_report plans one call", () => {
  it("posts the child's report for its own lens and head, with the header T3 writes", () => {
    assert.deepStrictEqual(plan(), { type: "post", lens: "code", head: A, comment: COMMENT });
  });

  it("the lens and head are the stored child's: the newest child of the calling thread, never the input's", () => {
    const children = [child({ key: "data", prs: [{ number: 7, head: B }] }), child()];
    assert.deepStrictEqual(plan({ children }), {
      type: "post",
      lens: "code",
      head: A,
      comment: COMMENT,
    });
    const other = plan({ children: [child({ childThreadId: "thread-other" })] });
    assert.strictEqual(other.type, "refused");
    assert.include(
      other.type === "refused" ? other.reason : "",
      "no lens child lens_start started",
    );
    const unknown = plan({ children: [child({ childThreadId: null })] });
    assert.strictEqual(unknown.type, "refused");
  });

  it("a batch child posts for each of its pull requests, and for no other", () => {
    const children = [
      child({
        prs: [
          { number: 7, head: A },
          { number: 8, head: B },
        ],
      }),
    ];
    const eight = plan({ children, pr: 8, pull: pull({ number: 8, head: B }) });
    assert.deepStrictEqual(
      eight.type === "post" ? [eight.head, eight.comment.split("\n")[0]] : eight,
      [B, `lens: code · reviewer: model-x · read at: ${B}`],
    );
    assert.deepStrictEqual(plan({ children, pr: 9, pull: pull({ number: 9 }) }), {
      type: "refused",
      head: null,
      reason: "this lens child reads #7, #8, not #9",
    });
  });

  it("a head that moved, a closed or unknown pull request, or rules off: nothing is posted", () => {
    const moved = plan({ pull: pull({ head: B }) });
    assert.strictEqual(moved.type, "refused");
    assert.include(
      moved.type === "refused" ? moved.reason : "",
      `#7 is now at ${B}, not at ${A}, the head this lens read`,
    );
    assert.deepStrictEqual(plan({ pull: pull({ state: "merged" }) }), {
      type: "refused",
      head: A,
      reason: "#7 is merged",
    });
    assert.deepStrictEqual(plan({ pull: null }), {
      type: "refused",
      head: A,
      reason: "GitHub does not know #7",
    });
    const off = plan({ rules: effectiveReviewRules({ preset: "off" }) });
    assert.strictEqual(off.type, "refused");
  });

  it("a head GitHub gives in capitals is the same head", () => {
    assert.strictEqual(plan({ pull: pull({ head: A.toUpperCase() }) }).type, "post");
  });

  it("runs again: a report already posted at the head is that post, not a second one", () => {
    const earlier = pull({ posts: [post(COMMENT.replaceAll("\n", "\r\n") + "\r\n")] });
    assert.deepStrictEqual(plan({ pull: earlier }), {
      type: "posted",
      lens: "code",
      head: A,
      url: "https://example.test/pull/7#c1",
    });
    // An edited or hidden one does not count as the read: the report is posted again, as a new comment.
    assert.strictEqual(
      plan({ pull: pull({ posts: [post(COMMENT, { edited: true })] }) }).type,
      "post",
    );
    assert.strictEqual(
      plan({ pull: pull({ posts: [post(COMMENT, { hidden: true })] }) }).type,
      "post",
    );
    // Another report of the same lens and head is a new read: posted, the gate reads the newest.
    const other = pull({ posts: [post(`${COMMENT}\nminor: a typo`)] });
    assert.strictEqual(plan({ pull: other }).type, "post");
  });

  it("a report T3 would not post is refused with the head it is for", () => {
    const got = plan({ body: "looks good" });
    assert.strictEqual(got.type, "refused");
    assert.strictEqual(got.type === "refused" ? got.head : null, A);
    assert.include(got.type === "refused" ? got.reason : "", "no findings section");
  });
});
