/**
 * A scan vector's posts (review.vectors.json `scans`) as the gate lists them (lens.Review.lens_posts), the same way
 * the Python side's scan() builds its pull request: each post by `someone`, one second apart unless it names its
 * `when`; a comment by default, else a `review` (its flags read unless `flags` is false) or an `inline` review comment.
 * The list holds the comments, then the reviews, then the inline comments, each kind in vector order.
 */
import type { LensPost } from "./lensComment.ts";

export interface VectorPost {
  readonly body: string;
  readonly edited?: boolean;
  readonly hidden?: boolean;
  readonly kind?: "comment" | "review" | "inline";
  readonly when?: string;
  readonly flags?: boolean;
}

const second = (i: number) =>
  `2026-01-01T00:${String(Math.floor(i / 60)).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}Z`;

export function vectorPosts(posts: ReadonlyArray<VectorPost>): LensPost[] {
  const listed = posts.map((p, i) => {
    const when = p.when ?? second(i);
    const post: LensPost = {
      body: p.body,
      edited: p.edited ?? false,
      hidden: p.hidden ?? false,
      where: `by someone at ${when || "?"}`,
      when,
      ...(p.kind === "review" && p.flags === false ? { unread: true } : {}),
    };
    return { post, rank: p.kind === "review" ? 1 : p.kind === "inline" ? 2 : 0 };
  });
  return listed.toSorted((a, b) => a.rank - b.rank).map(({ post }) => post);
}
