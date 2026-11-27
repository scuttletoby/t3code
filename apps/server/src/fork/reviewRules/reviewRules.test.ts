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
  NO_MARKER,
  REPOST,
  hasMarker,
  lensHeader,
  lensKey,
  lensProblem,
  lensScan,
  looksProblem,
  notLensReport,
  reportProblem,
} from "./lensComment.ts";
import { ReviewPlanError, globMatches, planReview, type ReviewBlock } from "./plan.ts";
import { PyTextError } from "./pyText.ts";
import { PY_ASSIGNED, PY_UNICODE } from "./pyUnicode.ts";
import { vectorPosts } from "./vectors.testkit.ts";
import {
  CLEARED,
  REPORT,
  judgeRounds,
  requiredLenses,
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
  scans: ["name", "posts", "history", "entries", "not_lens"],
  linear: ["name", "fn", "parts", "expect", "ms"],
  py_unicode: null,
  unicode: ["name", "body", "findings", "section", "unassigned", "refused"],
  // Run by lensStart.test.ts: lens_start's copy of lens_reads, review_rounds and verdict.
  reads: ["name", "posts", "head", "same", "override_authors", "required", "expect"],
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

const scan = (v: any) => lensScan(vectorPosts(v.posts), V.override_prefix);

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
      assert.deepStrictEqual(
        scan(s),
        { history: s.history, entries: s.entries, notLens: s.not_lens ?? [] },
        s.name,
      );
  });

  it("every round", () => {
    assert.isAtLeast(V.rounds.length, 28);
    for (const r of V.rounds) assert.deepStrictEqual(replayRound(r), r.expect, r.name);
  });

  // Each with its own time bound: the server's test timeout is 120 s, so a slow but not hanging path would pass it.
  it("every linear-time vector, in its time", () => {
    assert.isAtLeast(V.linear.length, 17);
    const fns: Record<string, (body: string) => unknown> = {
      read_findings: (body) => readFindings(body),
      lens_problem: (body) => lensProblem(body, V.override_prefix),
    };
    for (const v of V.linear) {
      const body = (v.parts as Array<[string, number]>).map(([text, n]) => text.repeat(n)).join("");
      assert.property(fns, v.fn, v.name);
      // oxlint-disable-next-line t3code/no-wall-clock-in-tests -- a linear-time bound, as the gate's own test has
      const t = performance.now();
      const got = fns[v.fn]!(body);
      // oxlint-disable-next-line t3code/no-wall-clock-in-tests -- the same bound
      assert.isBelow(performance.now() - t, v.ms, v.name);
      assert.deepStrictEqual(got, v.expect, v.name);
    }
  });

  it("the gate's Unicode table, and text whose code points this runtime assigns otherwise is refused", () => {
    assert.deepStrictEqual(
      [PY_UNICODE, PY_ASSIGNED],
      [V.py_unicode.version, V.py_unicode.assigned],
    );
    assert.isAtLeast(V.unicode.length, 3);
    let refused = 0;
    for (const v of V.unicode) {
      // Unassigned in the gate's Unicode and assigned here as a letter, number, mark, format character or space: those
      // read differently. A new symbol (an emoji) reads alike.
      const drift = (v.refused as string[]).filter(
        (u) => !/^\p{Cn}$/u.test(String.fromCodePoint(Number.parseInt(u.slice(2), 16))),
      );
      if (drift.length === 0) {
        assert.deepStrictEqual(readFindings(v.body), [v.findings, v.section], v.name);
        continue;
      }
      refused++;
      // A code point an HTML reference writes (`&#x1C89;`) is read only where the text is folded (the findings rules):
      // the header, key and marker rules read the text as written.
      const written = drift.filter((u) =>
        v.body.includes(String.fromCodePoint(Number.parseInt(u.slice(2), 16))),
      );
      const reads: Array<[(body: string) => unknown, string[]]> = [
        [readFindings, drift],
        [lensProblem, drift],
        [fold, drift],
        [lensHeader, written],
        [lensKey, written],
        [hasMarker, written],
      ];
      for (const [read, want] of reads) {
        if (want.length === 0) {
          assert.doesNotThrow(() => read(v.body), `${v.name}: ${read.name}`);
          continue;
        }
        assert.throws(() => read(v.body), PyTextError, undefined, `${v.name}: ${read.name}`);
        try {
          read(v.body);
        } catch (error) {
          assert.deepStrictEqual(
            (error as PyTextError).codePoints,
            want,
            `${v.name}: ${read.name}`,
          );
        }
      }
    }
    // This runtime's Unicode is newer than 15.0 (Node 24: 16 or later), so the new letter is refused.
    assert.isAtLeast(refused, 1);
  });

  it("every recorded lens_header and lens_key call of the gate's corpus", () => {
    const fns: Record<string, (...args: any[]) => unknown> = {
      lens_header: (body: string) => lensHeader(body),
      lens_key: lensKey,
    };
    const cases = CORPUS.filter((c) => c.fn in fns);
    assert.strictEqual(cases.length, 528 + 46);
    // A line is read in NFKC, so `read at：<sha>` reads its sha where the source read none: the one recorded answer
    // that changes on purpose (lens.test.py's Corpus counts it too). Its sha is short, so it still reads nothing.
    const folded = cases.filter(
      (c) =>
        c.fn === "lens_header" && c.args[0].normalize("NFKC") !== c.args[0] && c.out?.[1] === null,
    );
    assert.deepStrictEqual(
      folded.map((c) => c.args[0]),
      ["lens: correctness · reviewer: lens-x (Opus xhigh) · read at\uff1aabc0abc"],
    );
    for (const c of cases) {
      const want = folded.includes(c) ? [c.out[0], "abc0abc"] : c.out;
      assert.deepStrictEqual(fns[c.fn]!(...c.args), want, JSON.stringify(c.args));
    }
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
    assert.deepStrictEqual(lensScan([]), { history: [], entries: {}, notLens: [] });
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
  it("a look-alike without the lens marker is no lens report; one with it still blocks (#865)", () => {
    const near = scan(
      named("scans", "a lens heading with no lens marker is no lens report (issue 865)"),
    );
    assert.deepStrictEqual([near.history, near.entries], [[], {}]);
    assert.lengthOf(near.notLens, 1);
    assert.include(near.notLens[0]![1], NO_MARKER);
    const read = scan(
      named("scans", "a lens heading that names a read carries the marker and reads nothing"),
    );
    assert.isTrue(Object.keys(read.entries)[0]!.startsWith(NO_HEADER));
    assert.deepStrictEqual(read.notLens, []);
    const beside = scan(
      named("scans", "a near miss beside a report of the same lens and head leaves the read"),
    );
    assert.deepStrictEqual(beside.entries, { code: ["a".repeat(40), null] });
    for (const body of [
      "## Lens A\nmajor: none",
      "lgtm, safe to merge",
      "Here.\nread at: 1234567",
    ]) {
      const looks = looksProblem(body);
      assert.isNotNull(looks, body);
      assert.strictEqual(lensProblem(body), hasMarker(body) ? looks : null, body);
      assert.strictEqual(notLensReport(body) !== null, !hasMarker(body), body);
    }
  });
  it("a look-alike without the marker that holds an open finding blocks, names the fix, and an older read does not win (#865 round 1)", () => {
    for (const body of [
      "## Code lens\nblocker: a re-run loses data\nmajor: none",
      "covered: a\nchecks: b\nmajor: the retry drops a row",
      "NOT SAFE to merge (code lens, round 2)\nmore below",
    ]) {
      const got = reportProblem(body);
      assert.strictEqual(got?.[1], "held", body);
      assert.strictEqual(lensProblem(body), got![0], body);
      assert.isNull(notLensReport(body), body);
      assert.isTrue(got![0].startsWith(`${NO_MARKER}, but it holds an open `), got![0]);
      assert.include(got![0], REPOST);
    }
    assert.strictEqual(reportProblem("## Code lens\nAll fine.")?.[1], "notLens");
    assert.strictEqual(reportProblem("Here.\nread at: 1234567")?.[1], "broken");
    assert.isNull(reportProblem(`lens: code · reviewer: a · read at: ${"a".repeat(40)}`));
    for (const name of [
      "a clean read then a newer unmarked report with an open blocker holds the head (issue 865)",
      "a clean read then a newer unmarked NOT SAFE verdict holds the head",
    ]) {
      const got = scan(named("scans", name));
      assert.deepStrictEqual(got.entries.code, ["a".repeat(40), null], name);
      const held = Object.keys(got.entries).filter((key) => key.startsWith(NO_HEADER));
      assert.lengthOf(held, 1, name);
      assert.include(held[0], REPOST);
      assert.deepStrictEqual(got.notLens, [], name);
    }
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
});
