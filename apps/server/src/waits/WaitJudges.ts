/**
 * Fork (waits): the judges, one per kind, that read a condition once. A judge never fails: an error
 * is a `failed` judgement, and the wait service decides what it means. This PR has the `file`
 * judge; the `check`, `unit` and `pr` judges come with their own PRs.
 */
import type { WaitKind, WaitTarget } from "@t3tools/contracts/waitFor";
import type { Judgement } from "@t3tools/shared/waitFor";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import type * as PlatformError from "effect/PlatformError";

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

/** The system error code (`ENOTDIR`, `EACCES`, ...), when there is one. */
const errorCode = (error: PlatformError.PlatformError): string | undefined => {
  const cause = error.reason.cause;
  const code =
    typeof cause === "object" && cause !== null && "code" in cause
      ? (cause as { readonly code: unknown }).code
      : undefined;
  return typeof code === "string" && /^[A-Z0-9_]{1,40}$/u.test(code) ? code : undefined;
};

/**
 * A failed `stat` without the path: the stored wait and its message already name the path, and
 * a path repeated in the error would only grow the store.
 */
const statErrorText = (error: PlatformError.PlatformError): string => {
  const code = errorCode(error);
  return `The file cannot be checked: ${error.reason._tag}${code === undefined ? "" : ` (${code})`}.`;
};

/**
 * Exists or gone, as `stat` sees it: a link to nothing is gone, and so is a path whose parent
 * folder was replaced by a file.
 */
export const makeFileJudge = Effect.fn("WaitJudges.makeFileJudge")(function* () {
  const fs = yield* FileSystem.FileSystem;
  const judge: WaitJudge<"file"> = ({ spec }) =>
    fs.stat(spec.path).pipe(
      Effect.as(true),
      Effect.catchTags({
        PlatformError: (error) =>
          // ENOTDIR: a folder on the path is now a file, so nothing can be at the path.
          error.reason._tag === "NotFound" || errorCode(error) === "ENOTDIR"
            ? Effect.succeed(false)
            : Effect.fail(error),
      }),
      Effect.map((exists): Judgement => {
        if (exists === (spec.until === "exists")) {
          return {
            type: "met",
            outcome: spec.until,
            detail: exists ? "It exists." : "It is gone.",
          };
        }
        return { type: "notYet", detail: exists ? "It still exists." : "It does not exist yet." };
      }),
      Effect.catch((error) =>
        Effect.succeed<Judgement>({
          type: "failed",
          message: statErrorText(error),
          permanent: false,
        }),
      ),
    );
  return judge;
});
