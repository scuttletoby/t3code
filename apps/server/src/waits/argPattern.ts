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
  | { readonly type: "slow" };

const SCRIPT = `
const { parentPort, workerData } = require("node:worker_threads");
const pattern = new RegExp("^(?:" + workerData.pattern + ")$", "u");
const bad = workerData.args.find((arg) => !pattern.test(arg));
parentPort.postMessage(bad === undefined ? null : bad);
`;

const kept = new Map<string, ArgPatternAnswer>();
const workers = Semaphore.makeUnsafe(WORKERS);

const runWorker = (pattern: string, args: ReadonlyArray<string>, limitMs: number) =>
  Effect.callback<ArgPatternAnswer>((resume) => {
    let done = false;
    const answer = (value: ArgPatternAnswer) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      void worker.terminate();
      resume(Effect.succeed(value));
    };
    const worker = new NodeWorkerThreads.Worker(SCRIPT, {
      eval: true,
      workerData: { pattern, args: [...args] },
      resourceLimits: { maxOldGenerationSizeMb: 32, maxYoungGenerationSizeMb: 8 },
    });
    const timer = setTimeout(() => answer({ type: "slow" }), limitMs);
    worker.once("message", (bad: unknown) =>
      answer(typeof bad === "string" ? { type: "bad", arg: bad } : { type: "ok" }),
    );
    // A pattern that does not compile (settings refuse one) or a worker out of memory: not met.
    worker.once("error", () => answer({ type: "bad", arg: args[0] ?? "" }));
    worker.once("exit", () => answer({ type: "slow" }));
    return Effect.sync(() => {
      done = true;
      clearTimeout(timer);
      void worker.terminate();
    });
  });

/**
 * Whether every argument matches the whole pattern. No arguments: "ok" without a worker. A slow
 * answer is not kept, so a busy moment is not remembered as a bad pattern.
 */
export const testArgPattern = (
  pattern: string,
  args: ReadonlyArray<string>,
  limitMs: number = ARG_PATTERN_LIMIT_MS,
): Effect.Effect<ArgPatternAnswer> =>
  Effect.suspend(() => {
    if (args.length === 0) return Effect.succeed<ArgPatternAnswer>({ type: "ok" });
    const key = JSON.stringify([pattern, args]);
    const known = kept.get(key);
    if (known !== undefined) return Effect.succeed(known);
    return workers
      .withPermits(1)(runWorker(pattern, args, limitMs))
      .pipe(
        Effect.tap((answer) =>
          Effect.sync(() => {
            if (answer.type === "slow") return;
            if (kept.size >= KEPT_ANSWERS) kept.delete(kept.keys().next().value!);
            kept.set(key, answer);
          }),
        ),
      );
  });
