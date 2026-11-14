// @effect-diagnostics nodeBuiltinImport:off -- the file check process is an external runtime boundary.
/**
 * Fork (waits): the judges, one per kind, that read a condition once. A judge never fails: an error
 * is a `failed` judgement, and the wait service decides what it means. This PR has the `file`
 * judge, in the server and in a short-lived process; the `check`, `unit` and `pr` judges come with
 * their own PRs.
 */
import * as NodeChildProcess from "node:child_process";
import type { WaitKind, WaitTarget } from "@t3tools/contracts/waitFor";
import * as HostProcess from "@t3tools/shared/HostProcess";
import type { Judgement } from "@t3tools/shared/waitFor";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import type * as PlatformError from "effect/PlatformError";

import { WAIT_STAT_PATH_ENV, WAIT_STAT_SCRIPT } from "./waitStatChild.ts";

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

/** Met or not yet, from whether the file exists. */
const fileJudgement = (
  spec: Extract<WaitTarget, { readonly kind: "file" }>["spec"],
  exists: boolean,
): Judgement => {
  if (exists === (spec.until === "exists")) {
    return {
      type: "met",
      outcome: spec.until,
      detail: exists ? "It exists." : "It is gone.",
    };
  }
  return { type: "notYet", detail: exists ? "It still exists." : "It does not exist yet." };
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
      Effect.map((exists) => fileJudgement(spec, exists)),
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

/** A file check process is killed when it has not answered by then. */
export const FILE_CHILD_LIMIT_MS = 10_000;
/** The most a file check process may print; its answer is one short line. */
const FILE_CHILD_MAX_OUTPUT = 1024;

/**
 * `makeFileJudge` in a short-lived process killed after `limitMs`: a hung `stat` holds no thread of
 * the server's I/O pool, and a process can be killed where a thread cannot. A process that does not
 * end even when killed keeps its place (the service counts it as stuck). `command` is for tests.
 */
export const makeChildFileJudge = Effect.fn("WaitJudges.makeChildFileJudge")(function* (options?: {
  readonly limitMs?: number;
  readonly command?: { readonly file: string; readonly args: ReadonlyArray<string> };
}) {
  const executable = yield* HostProcess.ExecutablePath;
  const isExecutable = yield* HostProcess.IsExecutable;
  const environment = yield* HostProcess.Environment;
  const limitMs = options?.limitMs ?? FILE_CHILD_LIMIT_MS;
  const command = options?.command ?? {
    file: executable,
    // A packaged single executable takes no `-e`; it runs the same check as `__wait-stat`.
    args: isExecutable ? ["__wait-stat"] : ["-e", WAIT_STAT_SCRIPT],
  };
  const failed = (message: string): Judgement => ({ type: "failed", message, permanent: false });
  const judge: WaitJudge<"file"> = ({ spec }) =>
    Effect.callback<Judgement>((resume) => {
      let answered = false;
      const answer = (judgement: Judgement) => {
        if (answered) return;
        answered = true;
        resume(Effect.succeed(judgement));
      };
      let child: NodeChildProcess.ChildProcess;
      try {
        child = NodeChildProcess.spawn(command.file, [...command.args], {
          stdio: ["ignore", "pipe", "ignore"],
          windowsHide: true,
          // Node kills it at the limit; its answer then comes in "close".
          timeout: limitMs,
          killSignal: "SIGKILL",
          env: { ...environment, ELECTRON_RUN_AS_NODE: "1", [WAIT_STAT_PATH_ENV]: spec.path },
        });
      } catch (error) {
        answer(failed(`The file check process could not start: ${String(error).slice(0, 200)}`));
        return;
      }
      let output = "";
      child.stdout?.setEncoding("utf8");
      child.stdout?.on("data", (chunk: string) => {
        if (output.length < FILE_CHILD_MAX_OUTPUT) output += chunk;
      });
      child.once("error", (error) => {
        // Only a process that never started ends here; one that ran ends in "close".
        if (child.pid !== undefined) return;
        const code = (error as { readonly code?: unknown }).code;
        answer(
          failed(
            `The file check process could not start${typeof code === "string" && /^[A-Z0-9_]{1,40}$/u.test(code) ? ` (${code})` : ""}.`,
          ),
        );
      });
      child.once("close", (exitCode, signal) => {
        if (signal === "SIGKILL") {
          answer(failed(`The file check gave no answer in ${limitMs / 1000}s.`));
          return;
        }
        answer(parseChildAnswer(spec, output, exitCode));
      });
      // Only when the service itself stops it (it does not, but a scope may end).
      return Effect.sync(() => {
        if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
      });
    });
  return judge;
});

function parseChildAnswer(
  spec: Extract<WaitTarget, { readonly kind: "file" }>["spec"],
  output: string,
  exitCode: number | null,
): Judgement {
  let parsed: unknown;
  try {
    parsed = JSON.parse(output.trim());
  } catch {
    parsed = null;
  }
  if (typeof parsed === "object" && parsed !== null) {
    if ("exists" in parsed && parsed.exists === true) return fileJudgement(spec, true);
    if ("code" in parsed && typeof parsed.code === "string") {
      // ENOTDIR: a folder on the path is now a file, so nothing can be at the path.
      if (parsed.code === "ENOENT" || parsed.code === "ENOTDIR") return fileJudgement(spec, false);
      const code = /^[A-Z0-9_]{1,40}$/u.test(parsed.code) ? ` (${parsed.code})` : "";
      return { type: "failed", message: `The file cannot be checked${code}.`, permanent: false };
    }
  }
  return {
    type: "failed",
    message: `The file check process gave no answer (exit code ${exitCode ?? "none"}).`,
    permanent: false,
  };
}
