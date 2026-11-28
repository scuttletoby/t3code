/**
 * Fork: `lens_report`'s rules. T3 writes a report's first line (lens, reviewer, the head the child was started for)
 * and posts only while the pull request is at that head and the gate reads the comment as one valid read. Pure: the
 * server (lensStartServer.ts) reads GitHub and the stored children, and posts.
 */
import type { EffectiveReviewRules } from "@t3tools/contracts";
import * as Schema from "effect/Schema";

import { readFindings } from "./findings.ts";
import { hasMarker, lensHeader, lensKey, lensProblem } from "./lensComment.ts";
import type { PullRead, StoredChild } from "./lensStartRun.ts";
import { PyTextError } from "./pyText.ts";

export const LENS_REPORT_TOOL = "lens_report";
export const LENS_REPORT_OFF =
  'T3 refused "lens_report": review rules are off for this thread\'s project (Settings: review rules). Report nothing; say so in your answer.';

const isPyTextError = Schema.is(PyTextError);

/** The longest reviewer name the header carries; a longer one is cut. */
const REVIEWER_MAX = 80;

/** The reviewer as one short line: the model the child runs on. */
export const reviewerName = (model: string): string =>
  model.replace(/\s+/gu, " ").trim().slice(0, REVIEWER_MAX).trim() || "unknown";

/** The first line T3 writes: the lens header the gate reads (COMMENTS.md, "The lens header"). */
export const lensReportHeader = (lens: string, reviewer: string, head: string): string =>
  `lens: ${lens} · reviewer: ${reviewerName(reviewer)} · read at: ${head}`;

export type LensReportWritten =
  | { readonly type: "written"; readonly comment: string }
  | { readonly type: "refused"; readonly reason: string };

/**
 * The comment for a report: T3's header, a blank line, the report without its leading blank lines and trailing
 * space. Refused when the report is empty, holds a lens marker itself, is longer than `maxChars` with its header, or
 * would not be one valid read of `lens` at `head` with a findings section or finding (the gate would not count it).
 */
export function writeLensReport(args: {
  readonly lens: string;
  readonly reviewer: string;
  readonly head: string;
  readonly body: string;
  readonly maxChars: number;
}): LensReportWritten {
  const refused = (reason: string): LensReportWritten => ({ type: "refused", reason });
  const body = args.body.replace(/^(?:[ \t]*(?:\r\n|\r|\n))+/u, "").trimEnd();
  if (body === "") return refused("the report is empty: write its findings section");
  const comment = `${lensReportHeader(args.lens, args.reviewer, args.head)}\n\n${body}`;
  try {
    if (hasMarker(body)) {
      return refused(
        "the report holds a lens marker line (a line starting `lens:`, or a `read at` or lens field naming a " +
          "sha): T3 writes the report's first line, so leave it out, and name a sha in prose without `read at`",
      );
    }
    if (comment.length > args.maxChars) {
      return refused(
        `the report is ${comment.length} characters with its first line, more than the ${args.maxChars} the ` +
          "review rules allow: shorten it",
      );
    }
    const header = lensHeader(comment);
    if (header === null || lensKey(header[0]) !== args.lens || header[1] !== args.head) {
      return refused(
        `T3's first line does not read as lens ${args.lens} at ${args.head} once your report follows it`,
      );
    }
    const problem = lensProblem(comment);
    if (problem !== null)
      return refused(`the gate would not read it as one valid read: ${problem}`);
    const [said, section] = readFindings(comment);
    if (said.length === 0 && !section) {
      return refused(
        "it has no findings section, so the gate cannot judge it: write `blocker: none` and `major: none` when " +
          "there is none, else one line per finding starting `blocker:` or `major:`",
      );
    }
  } catch (error) {
    if (!isPyTextError(error)) throw error;
    return refused(`${error.message}: write those characters another way`);
  }
  return { type: "written", comment };
}

export type LensReportPlan =
  | {
      readonly type: "post";
      readonly lens: string;
      readonly head: string;
      readonly comment: string;
    }
  | {
      readonly type: "posted";
      readonly lens: string;
      readonly head: string;
      readonly url: string | null;
    }
  | { readonly type: "refused"; readonly head: string | null; readonly reason: string };

/** A post as written, for a repeat: line ends and trailing space as GitHub may store them. */
const same = (a: string, b: string) =>
  a.replace(/\r\n?/gu, "\n").trimEnd() === b.replace(/\r\n?/gu, "\n").trimEnd();

/**
 * What `lens_report` does for one call. The lens and head are those of the calling thread's newest stored child,
 * never the input's. Refused: rules off, no such child or pull request, closed, moved, or a report writeLensReport
 * refuses. An unedited comment of the same text is `posted`: a repeat posts nothing.
 */
export function planLensReport(input: {
  readonly rules: EffectiveReviewRules;
  readonly caller: string;
  readonly reviewer: string;
  readonly pr: number;
  readonly body: string;
  readonly children: ReadonlyArray<StoredChild>;
  readonly pull: PullRead | null;
}): LensReportPlan {
  if (input.rules.preset === "off") {
    return {
      type: "refused",
      head: null,
      reason: "review rules are off for this project (Settings: review rules)",
    };
  }
  const child = input.children.findLast((c) => c.childThreadId === input.caller);
  if (child === undefined) {
    return {
      type: "refused",
      head: null,
      reason:
        "this thread is no lens child lens_start started here: only such a child posts with lens_report, for " +
        "its own lens and head",
    };
  }
  const read = child.prs.find((p) => p.number === input.pr);
  if (read === undefined) {
    return {
      type: "refused",
      head: null,
      reason: `this lens child reads ${child.prs.map((p) => `#${p.number}`).join(", ")}, not #${input.pr}`,
    };
  }
  const refused = (reason: string): LensReportPlan => ({
    type: "refused",
    head: read.head,
    reason,
  });
  if (input.pull === null) return refused(`GitHub does not know #${input.pr}`);
  if (input.pull.state !== "open") return refused(`#${input.pr} is ${input.pull.state}`);
  if (input.pull.head.toLowerCase() !== read.head) {
    return refused(
      `#${input.pr} is now at ${input.pull.head}, not at ${read.head}, the head this lens read: a report of ` +
        "an old head does not count, so report nothing for it and say so in your answer",
    );
  }
  const written = writeLensReport({
    lens: child.key,
    reviewer: input.reviewer,
    head: read.head,
    body: input.body,
    maxChars: input.rules.reportMaxChars,
  });
  if (written.type === "refused") return refused(written.reason);
  const earlier = input.pull.posts.find(
    (post) => post.edited !== true && post.hidden !== true && same(post.body, written.comment),
  );
  if (earlier !== undefined) {
    return { type: "posted", lens: child.key, head: read.head, url: earlier.url };
  }
  return { type: "post", lens: child.key, head: read.head, comment: written.comment };
}
