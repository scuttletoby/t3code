/**
 * A vector's posts (`scans`, `reads`) as the gate lists them, built as review_judge.test.py's vector_pr() builds them:
 * comments, then reviews, then inline comments, each by its `author` (else `someone`), one second apart unless `when`.
 * With `overridePrefix`, a valid override comment (lens.Review.override_sha) carries the sha it vouches for.
 */
import type { LensStartPost } from "./lensStart.ts";

export interface VectorPost {
  readonly body: string;
  readonly edited?: boolean;
  readonly hidden?: boolean;
  readonly kind?: "comment" | "review" | "inline";
  readonly when?: string;
  readonly flags?: boolean;
  readonly author?: string;
}

const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");

const second = (i: number) =>
  `2026-01-01T00:${String(Math.floor(i / 60)).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}Z`;

export function vectorPosts(
  posts: ReadonlyArray<VectorPost>,
  overridePrefix?: string,
): LensStartPost[] {
  const valid =
    overridePrefix === undefined
      ? null
      : new RegExp(`^${escape(overridePrefix)} ([0-9a-f]{40}):`, "u");
  const listed = posts.map((p, i) => {
    const when = p.when ?? second(i);
    const author = p.author ?? "someone";
    const unread = p.kind === "review" && p.flags === false;
    const sha = !p.edited && !p.hidden && !unread ? valid?.exec(p.body)?.[1] : undefined;
    const post: LensStartPost = {
      body: p.body,
      edited: p.edited ?? false,
      hidden: p.hidden ?? false,
      where: `by ${author} at ${when || "?"}`,
      when,
      author,
      ...(unread ? { unread: true } : {}),
      ...(sha === undefined ? {} : { override: sha }),
    };
    return { post, rank: p.kind === "review" ? 1 : p.kind === "inline" ? 2 : 0 };
  });
  return listed.toSorted((a, b) => a.rank - b.rank).map(({ post }) => post);
}
