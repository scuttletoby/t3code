// @effect-diagnostics nodeBuiltinImport:off globalTimersInEffect:off globalJsonInEffect:off -- an owner's pattern runs in a worker thread, stopped by a timer; answers are kept by a JSON key.
/**
 * Fork (waits): an owner's `argPattern` against an agent's arguments, in a worker thread stopped at
 * `ARG_PATTERN_LIMIT_MS`, so a pattern that backtracks without end (`(a+)+$`) cannot hold the
 * server's event loop. Answers are kept, so a wait judged every 30 s starts no new worker.
 */
import * as NodeWorkerThreads from "node:worker_threads";
import * as Effect from "effect/Effect";
import * as Semaphore from "effect/Semaphore";

/** A worker, started and with its answer, that has taken longer than this is stopped. */
export const ARG_PATTERN_LIMIT_MS = 2_000;
/** Workers at once; more wait for a free one. */
const WORKERS = 2;
/** Answers kept, the oldest dropped first. */
const KEPT_ANSWERS = 512;

export type ArgPatternAnswer =
  | { readonly type: "ok" }
  /** The first argument the pattern does not match. */
  | { readonly type: "bad"; readonly arg: string }
  /** No answer in time: the pattern is too slow, or the server is very busy. */
  | { readonly type: "slow" }
  /**
   * The worker could not start or run (low memory, a thread limit): nothing is known about the
   * arguments, so it is a passing error, never kept.
   */
  | { readonly type: "failed"; readonly message: string };

/** Starts the worker that runs `SCRIPT`; tests give one that cannot start. */
export type StartArgPatternWorker = (
  script: string,
  options: NodeWorkerThreads.WorkerOptions,
) => NodeWorkerThreads.Worker;

const startWorker: StartArgPatternWorker = (script, options) =>
  new NodeWorkerThreads.Worker(script, options);

const SCRIPT = `
const { parentPort, workerData } = require("node:worker_threads");
const pattern = new RegExp("^(?:" + workerData.pattern + ")$", "u");
const bad = workerData.args.find((arg) => !pattern.test(arg));
parentPort.postMessage(bad === undefined ? null : bad);
`;

const kept = new Map<string, ArgPatternAnswer>();
const workers = Semaphore.makeUnsafe(WORKERS);

/** A worker error as one short line, without the arguments. */
const workerErrorText = (error: unknown): string => {
  const code = (error as { readonly code?: unknown } | null)?.code;
  return typeof code === "string" && /^[A-Z0-9_]{1,40}$/u.test(code) ? code : "an error";
};

const runWorker = (
  pattern: string,
  args: ReadonlyArray<string>,
  limitMs: number,
  start: StartArgPatternWorker,
) =>
  Effect.callback<ArgPatternAnswer>((resume) => {
    let done = false;
    let worker: NodeWorkerThreads.Worker | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const answer = (value: ArgPatternAnswer) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      void worker?.terminate();
      resume(Effect.succeed(value));
    };
    try {
      worker = start(SCRIPT, {
        eval: true,
        workerData: { pattern, args: [...args] },
        resourceLimits: { maxOldGenerationSizeMb: 32, maxYoungGenerationSizeMb: 8 },
      });
    } catch (error) {
      answer({
        type: "failed",
        message: `The pattern test could not start (${workerErrorText(error)}).`,
      });
      return;
    }
    timer = setTimeout(() => answer({ type: "slow" }), limitMs);
    worker.once("message", (bad: unknown) =>
      answer(typeof bad === "string" ? { type: "bad", arg: bad } : { type: "ok" }),
    );
    // The worker could not start, or ran out of memory (settings refuse a pattern that does not
    // compile): nothing is known about the arguments.
    worker.once("error", (error) =>
      answer({ type: "failed", message: `The pattern test failed (${workerErrorText(error)}).` }),
    );
    worker.once("exit", () => answer({ type: "slow" }));
    return Effect.sync(() => {
      done = true;
      clearTimeout(timer);
      void worker?.terminate();
    });
  });

/**
 * Whether every argument matches the whole pattern. No arguments: "ok" without a worker. Only "ok"
 * and "bad" are kept, so a busy moment or a worker that could not start is not remembered as a bad
 * pattern. `start` is for tests.
 */
export const testArgPattern = (
  pattern: string,
  args: ReadonlyArray<string>,
  limitMs: number = ARG_PATTERN_LIMIT_MS,
  start: StartArgPatternWorker = startWorker,
): Effect.Effect<ArgPatternAnswer> =>
  Effect.suspend(() => {
    if (args.length === 0) return Effect.succeed<ArgPatternAnswer>({ type: "ok" });
    const key = JSON.stringify([pattern, args]);
    const known = kept.get(key);
    if (known !== undefined) return Effect.succeed(known);
    return workers
      .withPermits(1)(runWorker(pattern, args, limitMs, start))
      .pipe(
        Effect.tap((answer) =>
          Effect.sync(() => {
            if (answer.type !== "ok" && answer.type !== "bad") return;
            if (kept.size >= KEPT_ANSWERS) kept.delete(kept.keys().next().value!);
            kept.set(key, answer);
          }),
        ),
      );
  });
