// @effect-diagnostics nodeBuiltinImport:off
// Fork: the T3 side of the review rules replays the CI gate's own vectors (one source, never copied):
// fork-ci/actions/pr-queue/review.vectors.json and the gate's lens corpus. A section or field this side does not run
// fails here, so a vector added for the Python gate cannot be skipped by T3.
import * as NodeFS from "node:fs";
import { assert, describe, it } from "@effect/vitest";

import { fold, readFindings } from "./findings.ts";
import {
  EDITED,
  EMBEDDED,
  HIDDEN,
  NO_HEADER,
  lensHeader,
  lensKey,
  lensProblem,
  lensScan,
} from "./lensComment.ts";
import { ReviewPlanError, globMatches, planReview, type ReviewBlock } from "./plan.ts";
import { repr, splitlines } from "./pyText.ts";
import {
  CLEARED,
  LOOSE,
  REPORT,
  judgeRounds,
  requiredLenses,
  roundMarker,
  roundsText,
  type EntryHow,
} from "./rounds.ts";

// Each read names its whole path: both files are `!` lines of .github/fork-owned-paths.txt, the rest of the folder is not.
const gate = (path: string) =>
  NodeFS.readFileSync(new URL(`../../../../../${path}`, import.meta.url), "utf8");
const V = JSON.parse(gate("fork-ci/actions/pr-queue/review.vectors.json"));
const CORPUS = gate("fork-ci/actions/pr-queue/testdata/lens-corpus.jsonl")
  .split("\n")
  .filter(Boolean)
  .map((line) => JSON.parse(line));

// The fields each section's vectors may carry; the Python side checks the same list (review_judge.test.py).
const FIELDS: Record<string, ReadonlyArray<string> | null> = {
  about: null,
  config_path: null,
  override_prefix: null,
  configs: null,
  globs: null,
  cases: ["name", "config", "changes", "weight", "lenses", "hits", "protected"],
  findings: ["name", "group", "body", "findings", "section"],
  scans: ["name", "posts", "history", "entries"],
  rounds: [
    "name",
    "config",
    "history",
    "reports",
    "decided",
    "changes",
    "override_authors",
    "findings",
    "expect",
  ],
};
const configs = V.configs as Record<string, ReviewBlock>;
const HEAD = `lens: code · reviewer: x · read at: ${"a".repeat(40)}\n`;

function replayRound(v: any) {
  const review: ReviewBlock = {
    ...configs[v.config]!,
    override_authors: v.override_authors ?? [],
    findings: v.findings ?? true,
  };
  const changes = v.changes as Record<string, string> | undefined;
  const got = judgeRounds(
    review,
    v.history,
    v.reports as Record<string, [EntryHow, string]>,
    v.decided ?? false,
    changes && ((sha: string) => changes[sha] ?? sha),
  );
  return {
    round: got.round,
    approach: got.approach,
    over_cap: got.overCap,
    findings: got.findings,
    problems: got.problems,
    text: roundsText(review, got),
  };
}

// Each post as the Python side's scan() posts it: by `someone`, one second apart, so the gate names it the same way.
const at = (i: number) =>
  `2026-01-01T00:${String(Math.floor(i / 60)).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}Z`;
const posts = (v: any) =>
  (v.posts as Array<{ body: string; edited?: boolean; hidden?: boolean }>).map((p, i) => ({
    body: p.body,
    edited: p.edited ?? false,
    hidden: p.hidden ?? false,
    where: `by someone at ${at(i)}`,
  }));
const scan = (v: any) => lensScan(posts(v), V.override_prefix);

describe("the shared vectors", () => {
  it("runs every section and every field, each vector name once", () => {
    assert.deepStrictEqual(Object.keys(V).toSorted(), Object.keys(FIELDS).toSorted());
    for (const [section, fields] of Object.entries(FIELDS)) {
      if (!fields) continue;
      const names = (V[section] as Array<{ name: string }>).map((v) => v.name);
      assert.strictEqual(new Set(names).size, names.length, `${section}: a name twice`);
      for (const v of V[section]) {
        for (const k of Object.keys(v))
          assert.include(fields, k, `${section} ${v.name}: field ${k}`);
      }
    }
  });

  it("every glob", () => {
    assert.isAtLeast(V.globs.length, 1);
    for (const [glob, path, want] of V.globs)
      assert.strictEqual(globMatches(glob, path), want, `${glob} ${path}`);
  });

  it("every case", () => {
    assert.isAtLeast(V.cases.length, 28);
    for (const c of V.cases) {
      const plan = planReview(configs[c.config]!, c.changes, V.config_path);
      assert.deepStrictEqual(
        {
          weight: plan.weight,
          lenses: plan.lenses,
          hits: plan.hits.map((h) => h.rule),
          protected: plan.protected,
        },
        { weight: c.weight, lenses: c.lenses, hits: c.hits, protected: c.protected },
        c.name,
      );
    }
  });

  it("every findings vector", () => {
    assert.isAtLeast(V.findings.length, 133);
    for (const f of V.findings) {
      assert.deepStrictEqual(
        readFindings(f.body),
        [f.findings, f.section],
        `${f.group}: ${f.name}`,
      );
    }
  });

  it("every scan", () => {
    assert.isAtLeast(V.scans.length, 31);
    for (const s of V.scans)
      assert.deepStrictEqual(scan(s), { history: s.history, entries: s.entries }, s.name);
  });

  it("every round", () => {
    assert.isAtLeast(V.rounds.length, 28);
    for (const r of V.rounds) assert.deepStrictEqual(replayRound(r), r.expect, r.name);
  });

  it("every recorded lens_header and lens_key call of the gate's corpus", () => {
    const fns: Record<string, (...args: any[]) => unknown> = {
      lens_header: (body: string) => lensHeader(body),
      lens_key: lensKey,
    };
    const cases = CORPUS.filter((c) => c.fn in fns);
    assert.strictEqual(cases.length, 528 + 46);
    for (const c of cases)
      assert.deepStrictEqual(fns[c.fn]!(...c.args), c.out, JSON.stringify(c.args));
  });
});

// The state question, by name: each is a shared vector, so both sides answer it alike.
describe("rounds meet old, repeated, edited, deleted and empty state", () => {
  const named = (section: string, name: string) => {
    const v = (V[section] as Array<{ name: string }>).find((x) => x.name === name);
    assert.isDefined(v, `${section}: ${name}`);
    return v as any;
  };
  it("the same comment read twice is one entry and one round", () => {
    const s = named("scans", "the same comment read twice");
    assert.deepStrictEqual(Object.keys(scan(s).entries), ["code"]);
    assert.strictEqual(
      replayRound(named("rounds", "the same comment read twice is one round")).round,
      1,
    );
  });
  it("an edited or hidden comment reads nothing but stays a round", () => {
    const edited = scan(
      named("scans", "an edited comment reads nothing and still counts in the history"),
    );
    assert.deepStrictEqual(edited.entries.code, [null, EDITED]);
    assert.strictEqual(edited.history.length, 2);
    const hidden = scan(
      named("scans", "a hidden comment reads nothing and still counts in the history"),
    );
    assert.deepStrictEqual(hidden.entries.code, [null, HIDDEN]);
  });
  it("a deleted older round counts only by a later round marker", () => {
    assert.strictEqual(
      replayRound(named("rounds", "a deleted older round no longer counts")).round,
      1,
    );
    assert.strictEqual(
      replayRound(named("rounds", "a deleted older round still counts by a later round marker"))
        .round,
      3,
    );
  });
  it("a pull request with no lens comments is round 0 and passes", () => {
    assert.deepStrictEqual(lensScan([]), { history: [], entries: {} });
    const r = replayRound(
      named("rounds", "a pull request with no lens comments is round 0 and passes"),
    );
    assert.deepStrictEqual([r.round, r.problems], [0, []]);
  });
  it("an old report from before findings were read (#740) has no section and blocks", () => {
    const r = replayRound(
      named("rounds", "an old report from before findings were read has no findings section"),
    );
    assert.lengthOf(r.problems, 1);
    assert.include(r.problems[0], "no findings section");
  });
  it("judging the same input again gives the same answer", () => {
    for (const r of V.rounds) assert.deepStrictEqual(replayRound(r), replayRound(r), r.name);
  });
  it("a post whose first line reads two ways reads nothing", () => {
    const got = scan(named("scans", "a first line that reads two ways reads nothing"));
    assert.deepStrictEqual(got.history, []);
    assert.deepStrictEqual(Object.values(got.entries), [[null, null]]);
    assert.isTrue(Object.keys(got.entries)[0]!.startsWith(NO_HEADER));
  });
  it("a second header under a valid one reads nothing, keeps the older read, and a new post does not clear it", () => {
    const quoted = scan(
      named(
        "scans",
        "a second lens header quoted under a valid one reads nothing; the older read stays",
      ),
    );
    assert.deepStrictEqual(quoted.history, [["code", "b".repeat(40)]]);
    assert.deepStrictEqual(quoted.entries.code, ["b".repeat(40), null]);
    const reposted = scan(
      named("scans", "a correct new post does not clear a post that reads nothing"),
    );
    assert.lengthOf(Object.keys(reposted.entries), 2);
    assert.include(Object.keys(reposted.entries)[0], EMBEDDED);
  });
  it("an override comment never reads as a lens report, with the repository's prefix only", () => {
    const body = named("scans", "an override comment that looks like a report is never one")
      .posts[0].body;
    assert.isNull(lensProblem(body, V.override_prefix));
    assert.isNotNull(lensProblem(body));
    assert.isNotNull(lensProblem(body, "another prefix"));
  });
});

describe("the T3 side's own edges", () => {
  const basic = configs.basic!;
  it("refuses what it cannot read rather than needing no lens", () => {
    for (const [changes, path] of [
      [[["X", "a"]], V.config_path],
      [[["R100", "a"]], V.config_path],
      [[["M", ""]], V.config_path],
      [[["M", "a"]], "/abs/path.json"],
      [[["M", "a"]], "a/../b.json"],
    ] as const) {
      assert.throws(() => planReview(basic, changes as any, path), ReviewPlanError);
    }
    const noCatchAll = { ...basic, rules: basic.rules.filter((r) => r.name !== "code") };
    assert.throws(
      () => planReview(noCatchAll, [["M", "src/x.ts"]], V.config_path),
      ReviewPlanError,
    );
    assert.throws(() => planReview(noCatchAll, [], V.config_path), ReviewPlanError);
  });

  it("adds the approach lens by key, once", () => {
    const review: ReviewBlock = { ...basic, override_authors: [] };
    const plan = planReview(review, [["M", "src/x.ts"]], V.config_path);
    const late = judgeRounds(
      review,
      [
        ["code", "a"],
        ["code", "b"],
        ["code", "c"],
      ],
      {
        code: [REPORT, `${HEAD}blocker: none\nmajor: none`],
      },
    );
    assert.deepStrictEqual([...requiredLenses(review, plan, late).values()], ["code", "approach"]);
    const decided = judgeRounds(
      review,
      [
        ["code", "a"],
        ["code", "b"],
        ["code", "c"],
      ],
      { code: [CLEARED, ""] },
      true,
    );
    assert.deepStrictEqual([...requiredLenses(review, plan, decided).values()], ["code"]);
  });

  // Each answer below is the gate's (review_judge.py), where JavaScript's own rules would differ.
  it("reads as Python's regex and int rules do", () => {
    const none = `${HEAD}blocker: none\nmajor: none\n`;
    assert.deepStrictEqual(readFindings(`${none}block\u0130ng: x`), [
      [["blocker", "block\u0130ng: x"]],
      true,
    ]);
    assert.deepStrictEqual(readFindings(`${none}verdict: not safe\u00e9`), [[], true]);
    assert.deepStrictEqual(readFindings(`${none}verdict: not safe!`), [
      [["blocker", "the report says NOT SAFE: verdict: not safe!"]],
      true,
    ]);
    assert.deepStrictEqual(readFindings(`${HEAD}blocker: none\n### Major\n## Next section\nnone`), [
      [["major", "Major (nothing under it)"]],
      true,
    ]);
    assert.strictEqual(roundMarker("code r\u0663"), 3);
    assert.strictEqual(roundMarker("code r\u0661\u0662"), 12);
    const review: ReviewBlock = { ...basic, override_authors: [] };
    const keys = judgeRounds(review, [], {
      "\u{1F600}": [LOOSE, "major: x"],
      "\uff42": [LOOSE, "major: y"],
    });
    assert.deepStrictEqual(
      keys.findings.map(([key]) => key),
      ["\uff42", "\u{1F600}"],
    );
  });

  it("reads Python's text rules", () => {
    assert.strictEqual(roundMarker("code r３"), 3);
    assert.strictEqual(roundMarker("code **r12**"), 12);
    assert.strictEqual(fold("ma&#106;or\\: x"), "major: x");
    assert.strictEqual(fold("majоr"), "major");
    assert.deepStrictEqual(splitlines("a\r\nb\u2028c\n"), ["a", "b", "c"]);
    assert.strictEqual(repr('it\'s "x" \\'), `'it\\'s "x" \\\\'`);
    assert.deepStrictEqual(readFindings(`${HEAD}blocker: none\nMAJOR: x`)[0], [
      ["major", "MAJOR: x"],
    ]);
    assert.deepStrictEqual(readFindings(`${HEAD}blocker: none\nmajor: x`)[0], [
      ["major", "major: x"],
    ]);
    assert.deepStrictEqual(readFindings(`${HEAD}blocker: none\nmajor: İx`)[0], [
      ["major", "major: İx"],
    ]);
  });

  // The gate's linear-time lines and its answers (review_judge.test.py), each with its own time bound: the server's
  // test timeout is 120 s, so a slow but not hanging path would pass without one.
  const timed = (f: () => unknown, ms: number) => {
    // oxlint-disable-next-line t3code/no-wall-clock-in-tests -- a linear-time bound, as the gate's own test has
    const t = performance.now();
    f();
    // oxlint-disable-next-line t3code/no-wall-clock-in-tests -- the same bound
    return assert.isBelow(performance.now() - t, ms);
  };
  it("reads a long run of spaces in linear time, as the gate does", () => {
    const n = 200_000;
    const sp = " ".repeat(n);
    const cases: Array<[string, Array<[string, string]>]> = [
      [`major${sp}x`, []],
      [`blocker: none${sp}x`, [["blocker", "blocker: none x"]]],
      [`| a | b |\n${sp}x`, []],
      [`| a | b |\n|${sp}x`, []],
      [`| a | b |\n|-${sp}x`, []],
      [`not${sp}safe`, [["blocker", "the report says NOT SAFE: not safe"]]],
      [`finding:${sp}x`, []],
      [`major: none${" \t".repeat(n / 2)}x`, [["major", "major: none x"]]],
    ];
    for (const [line, want] of cases)
      timed(() => assert.deepStrictEqual(readFindings(HEAD + line), [want, false]), 2000);
  });

  it("reads a wide table row in linear time, as the gate does", () => {
    // The old per-cell read took 39.5 s at 4,000 cells. Each about one full-size comment (65,536 characters).
    const rows = [
      "| " + "major | ".repeat(8000),
      "| " + "major | ".repeat(4000) + "x | ".repeat(4000),
      "| " + "major | ".repeat(3000) + "\n|" + "---|".repeat(3000) + "\n| " + "x | ".repeat(3000),
      "| major |" + " |".repeat(30_000) + " x |",
      "|" + "---|".repeat(16_000) + "x",
    ];
    for (const row of rows) timed(() => readFindings(HEAD + row), 2000);
    assert.deepStrictEqual(readFindings(HEAD + rows[0]!)[0], [["major", "major ".repeat(20)]]);
  });

  it("reads a full-size comment's lens problem in linear time", () => {
    const n = 16_000;
    for (const body of [
      `${HEAD}${"lens: x read at 1234567\n".repeat(n / 8)}`,
      `${HEAD}${"> ".repeat(n)}lens: code · read at: ${"a".repeat(40)}`,
      `x\n${" ".repeat(n * 4)}read at 1234567`,
      `not a lens: x\n${"> read at: 1234567\n".repeat(n / 8)}`,
    ])
      timed(() => lensProblem(body), 2000);
  });
});
