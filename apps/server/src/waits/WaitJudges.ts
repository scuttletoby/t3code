/**
 * Fork (waits): the judges, one per kind, that read a condition once. A judge never fails: an error
 * is a `failed` judgement, and the wait service decides what it means. This PR has the `file`
 * judge; the `check`, `unit` and `pr` judges come with their own PRs.
 */
import type { WaitKind, WaitTarget } from "@t3tools/contracts/waitFor";
import type { Judgement } from "@t3tools/shared/waitFor";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";

export interface WaitJudgeContext {
  /** The stored wait, or null when the condition is read once at registration. */
  readonly waitId: string | null;
  readonly now: number;
}

export type WaitJudge<K extends WaitKind = WaitKind> = (
  target: Extract<WaitTarget, { readonly kind: K }>,
  context: WaitJudgeContext,
) => Effect.Effect<Judgement>;

export type WaitJudges = { readonly [K in WaitKind]?: WaitJudge<K> };

/** Exists or gone, as `stat` sees it: a link to nothing is gone. */
export const makeFileJudge = Effect.fn("WaitJudges.makeFileJudge")(function* () {
  const fs = yield* FileSystem.FileSystem;
  const judge: WaitJudge<"file"> = ({ spec }) =>
    fs.stat(spec.path).pipe(
      Effect.as(true),
      Effect.catchTags({
        PlatformError: (error) =>
          error.reason._tag === "NotFound" ? Effect.succeed(false) : Effect.fail(error),
      }),
      Effect.map((exists): Judgement => {
        if (exists === (spec.until === "exists")) {
          return {
            type: "met",
            outcome: spec.until,
            detail: exists ? `${spec.path} exists.` : `${spec.path} is gone.`,
          };
        }
        return { type: "notYet", detail: exists ? "It still exists." : "It does not exist yet." };
      }),
      Effect.catch((error) =>
        Effect.succeed<Judgement>({ type: "failed", message: error.message, permanent: false }),
      ),
    );
  return judge;
});
