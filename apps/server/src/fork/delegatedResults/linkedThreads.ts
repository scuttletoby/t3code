/**
 * Fork (issues 386, 625): reads a linked environment's threads that a thread here delegated, page
 * by page (`t3_thread_list` lists the most recently updated first, at most 100 a page), for
 * `task_list` and the orphan scan at start.
 */
import type {
  EnvironmentId,
  OrchestratorMcpFailure,
  OrchestratorMcpThreadListResult,
} from "@t3tools/contracts";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";

import type { LinkedThread } from "./delegatedResults.ts";

export const LINKED_PAGE_SIZE = 100;

export interface LinkedThreadsRead {
  readonly threads: ReadonlyArray<LinkedThread>;
  /**
   * complete: every thread that may match was read. partial: the page limit was reached, or a
   * later page did not answer. unreachable: the first page did not answer.
   */
  readonly state: "complete" | "partial" | "unreachable";
}

export const listLinkedThreads = Effect.fn("delegatedResults.listLinkedThreads")(function* <R>(
  page: (
    cursor: number,
  ) => Effect.Effect<OrchestratorMcpThreadListResult, OrchestratorMcpFailure, R>,
  environmentId: EnvironmentId,
  options: {
    readonly maxPages: number;
    /** Each page's own limit; one that takes longer counts as no answer. */
    readonly timeout: Duration.Input;
    /** Stop after a page whose oldest thread was last updated before this (epoch ms). */
    readonly sinceMs?: number;
    /** Keep only threads made before this (epoch ms): a launch since then may still be recorded. */
    readonly createdBeforeMs?: number;
  },
) {
  const threads: Array<LinkedThread> = [];
  let cursor = 0;
  for (let read = 0; read < options.maxPages; read += 1) {
    const listed = yield* page(cursor).pipe(
      Effect.timeoutOption(options.timeout),
      Effect.orElseSucceed(() => ({ _tag: "None" }) as const),
    );
    if (listed._tag === "None") {
      return { threads, state: read === 0 ? "unreachable" : "partial" } satisfies LinkedThreadsRead;
    }
    for (const thread of listed.value.threads) {
      if (thread.delegatedFrom === undefined) continue;
      if (
        options.createdBeforeMs !== undefined &&
        !(Date.parse(thread.createdAt) < options.createdBeforeMs)
      ) {
        continue;
      }
      threads.push({
        environmentId,
        threadId: thread.threadId,
        title: thread.title,
        delegatedFrom: thread.delegatedFrom,
      });
    }
    const oldest = listed.value.threads.at(-1);
    if (
      listed.value.nextCursor === null ||
      listed.value.nextCursor <= cursor ||
      (options.sinceMs !== undefined &&
        oldest !== undefined &&
        Date.parse(oldest.updatedAt) < options.sinceMs)
    ) {
      return { threads, state: "complete" } satisfies LinkedThreadsRead;
    }
    cursor = listed.value.nextCursor;
  }
  return { threads, state: "partial" } satisfies LinkedThreadsRead;
});
