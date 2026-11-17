/**
 * Fork (playtest): sessions over one store file; docs/fork/playtest.md is the contract. One step at
 * a time per session, in this service's scope (a caller going away does not stop it). Claim and
 * push run in the worktree; renew and release in T3's own folder (a removed worktree never blocks).
 */
import {
  type PlaytestAdapter,
  type PlaytestEndReason,
  PLAYTEST_CONTROL_CHARACTERS,
  PLAYTEST_DETAIL_MAX,
  PLAYTEST_ENV,
  PLAYTEST_EXIT_OK,
  PLAYTEST_EXIT_REFUSED,
  PLAYTEST_NOTE_MAX,
  PlaytestFailure,
  type PlaytestFailureCode,
  type PlaytestSessionRecord,
  type PlaytestSettings,
  type PlaytestStep,
  PlaytestTarget,
  type ProjectId,
  type ThreadId,
} from "@t3tools/contracts";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import { resolveSpawnCommand } from "@t3tools/shared/shell";
import * as Cause from "effect/Cause";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Schema from "effect/Schema";
import * as Semaphore from "effect/Semaphore";

import * as ProcessRunner from "../processRunner.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import { shouldExcludeTerminalEnvKey } from "../terminal/Manager.ts";
import * as PlaytestStore from "./PlaytestStore.ts";

/** A step's output kept, on each of stdout and stderr: the last this many bytes. */
const STEP_MAX_OUTPUT_BYTES = 64 * 1024;
const RELEASE_PROBLEM = "release: ";
/** A release that cannot even start ends the session anyway; the claim expires on the target. */
export const RELEASE_COULD_NOT_RUN = "release could not run: ";
const SESSION_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567";

/** Where a thread's sessions run: its project, and its worktree (else the project's folder). */
export type PlaytestPlace =
  | { readonly type: "ok"; readonly projectId: ProjectId; readonly worktree: string }
  | { readonly type: "unavailable"; readonly message: string };

type OpKind = "claim" | "round" | "release";
interface Op {
  /** A failed claim's op goes on to release. */
  kind: OpKind;
  fiber: Fiber.Fiber<void> | undefined;
}

/** A session as callers see it: without the adapter's steps. */
export type PlaytestSessionView = Omit<PlaytestSessionRecord, "adapter"> & {
  /** The step kind running now, if any. */
  readonly running?: OpKind;
};

export type PlaytestStartResult = {
  /** `already-on`: this thread's session for this target was on; `stopped`: ended during the claim. */
  readonly outcome: "started" | "already-on" | "refused" | "failed" | "stopped";
  readonly session: PlaytestSessionView;
};

export type PlaytestPushResult = {
  readonly outcome: "done" | "refused" | "failed" | "stopped";
  readonly session: PlaytestSessionView;
};

export type PlaytestEndResult = {
  /** `release-failed`: the session stays `ending`; release is tried again later. */
  readonly outcome: "ended" | "nothing-to-end" | "release-failed";
  readonly session: PlaytestSessionView | null;
};

type StepResult = {
  readonly result: "ok" | "refused" | "failed";
  readonly detail: string;
  /** The program never ran: it is missing, not runnable, or would need a shell. */
  readonly notStarted?: true;
};

const failure = (code: PlaytestFailureCode, message: string) =>
  new PlaytestFailure({ code, message });

const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));
const decodeTarget = Schema.decodeUnknownOption(PlaytestTarget);

/** The end of `text`, so the step's last lines (where errors are) are kept (see `"tail"` output). */
function tail(text: string, max = PLAYTEST_DETAIL_MAX): string {
  const trimmed = text.trim();
  return trimmed.length <= max ? trimmed : `…${trimmed.slice(trimmed.length - max + 1)}`;
}

/** A value for a step's environment: a control character (a NUL above all) fails every spawn. */
const envText = (text: string) => text.replace(PLAYTEST_CONTROL_CHARACTERS, " ");

/** The server's environment without what T3's own terminal strips (T3CODE_*, PORT, …). */
function stepBaseEnv(): Record<string, undefined> {
  const strip: Record<string, undefined> = {};
  for (const key of Object.keys(process.env)) {
    if (shouldExcludeTerminalEnvKey(key)) strip[key] = undefined;
  }
  return strip;
}

function newSessionId(taken: ReadonlySet<string>): string {
  for (;;) {
    const bytes = crypto.getRandomValues(new Uint8Array(10));
    const id = `pt_${Array.from(bytes, (byte) => SESSION_ID_ALPHABET[byte % 32]).join("")}`;
    if (!taken.has(id)) return id;
  }
}

/** The steps a session starts with, or null when playtest is off or incomplete. */
function adapterOf(settings: PlaytestSettings): PlaytestAdapter | null {
  const { claim, renew, release, push } = settings;
  if (!settings.enabled || !claim || !renew || !release || push.length === 0) return null;
  const { renewMinutes, idleMinutes, maxHours } = settings;
  return { claim, renew, push, release, renewMinutes, idleMinutes, maxHours };
}

function view(session: PlaytestSessionRecord, running: OpKind | undefined): PlaytestSessionView {
  const { adapter: _adapter, ...rest } = session;
  return running === undefined ? rest : { ...rest, running };
}

/** Why a process could not run, in one line, without its command line. */
function runErrorText(error: ProcessRunner.ProcessRunError): string {
  switch (error._tag) {
    case "ProcessSpawnError":
      return "It could not start.";
    case "ProcessTimeoutError":
      return "Timed out.";
    case "ProcessOutputLimitError":
      return "Printed too much output.";
    case "ProcessReadError":
    case "ProcessStdinError":
      return `Its output could not be read (${error._tag}).`;
  }
}

export const make = Effect.fn("PlaytestService.make")(function* (input: {
  readonly filePath: string;
  /** T3's own folder, made when needed: renew and release run here, never in a worktree. */
  readonly stepsFolder: string;
  readonly placeOf: (threadId: ThreadId) => Effect.Effect<PlaytestPlace>;
}) {
  const settingsService = yield* ServerSettingsService;
  const runner = yield* ProcessRunner.ProcessRunner;
  const fs = yield* FileSystem.FileSystem;
  const store = yield* PlaytestStore.make({ filePath: input.filePath });
  /** Steps run in fibers of this scope: a server stop interrupts them (and kills each process). */
  const runScope = yield* Effect.scope;
  /** Guards `sessions`, `ops` and every store write. Never held while a step runs. */
  const lock = yield* Semaphore.make(1);
  let sessions: ReadonlyArray<PlaytestSessionRecord> | null = null;
  /** The one step run of each session, by session id; memory only, so empty after a restart. */
  const ops = new Map<string, Op>();

  const now = Effect.map(Clock.currentTimeMillis, iso);
  const locked = <A, E>(effect: Effect.Effect<A, E>) => lock.withPermits(1)(effect);

  const readSettings = settingsService.getSettings.pipe(
    Effect.mapError((error) =>
      failure("store_unavailable", `The settings cannot be read: ${error.message}`),
    ),
  );

  /** The sessions, read at the first use (call under `lock`). */
  const loaded = Effect.gen(function* () {
    if (sessions !== null) return sessions;
    const nowMs = yield* Clock.currentTimeMillis;
    const result = yield* store
      .load({ nowMs })
      .pipe(Effect.mapError((error) => failure("store_unavailable", error.message)));
    if (result._tag === "Newer") {
      return yield* failure(
        "store_unavailable",
        `${store.filePath} was written by a newer T3 (version ${result.version}); it is left as it is.`,
      );
    }
    if (result._tag === "Repaired") {
      yield* Effect.logError(
        `playtest: ${store.filePath} could not be read in full; it was saved as ${result.copyPath}. ${result.dropped} entries were dropped${result.lost.length === 0 ? "" : `, sessions not ended among them: ${result.lost.join(", ")} (their claims expire on the target)`}.`,
      );
    }
    // No step runs after a restart, so a round stored as running was cut by the stop.
    const at = iso(nowMs);
    sessions = result.sessions.map((session) =>
      session.lastRound?.state === "running"
        ? {
            ...session,
            lastRound: {
              ...session.lastRound,
              state: "stopped",
              detail: "T3 stopped while this round ran.",
              endedAt: at,
            },
          }
        : session,
    );
    return sessions;
  });

  /** Before any step runs: kept in memory only when the write worked. */
  const save = (next: ReadonlyArray<PlaytestSessionRecord>, keep: string) =>
    store.write(next, keep).pipe(
      Effect.tap((written) => Effect.sync(() => void (sessions = written))),
      Effect.mapError((error) => failure("store_unavailable", error.message)),
    );
  /** After a step ran: what happened is true either way, so memory keeps it; the next write saves it. */
  const record = Effect.fnUntraced(function* (
    next: ReadonlyArray<PlaytestSessionRecord>,
    keep: string,
  ) {
    sessions = next;
    yield* store.write(next, keep).pipe(
      Effect.tap((written) => Effect.sync(() => void (sessions = written))),
      Effect.catch((error) =>
        Effect.logWarning(`playtest: ${error.message}; the next change writes it.`),
      ),
    );
  });

  const find = (id: string) => (sessions ?? []).find((session) => session.id === id);
  /** The session a call worked on; writes never trim it, so it is there. */
  const found = (id: string) =>
    Effect.suspend(() => {
      const session = find(id);
      return session === undefined
        ? Effect.die(new Error(`playtest: session ${id} went missing from memory`))
        : Effect.succeed(session);
    });
  const replace = (next: PlaytestSessionRecord) =>
    (sessions ?? []).map((session) => (session.id === next.id ? next : session));
  const liveOf = (threadId: ThreadId) =>
    (sessions ?? []).find((session) => session.threadId === threadId && session.state !== "ended");
  const viewOf = (session: PlaytestSessionRecord) => view(session, ops.get(session.id)?.kind);

  /** Clears the session's op, unless another took its place. */
  const done = (id: string, op: Op) =>
    Effect.sync(() => {
      if (ops.get(id) === op) ops.delete(id);
    });

  /** Starts `body` as the session's one step run (call under `lock`). */
  const runOp = Effect.fnUntraced(function* (
    id: string,
    kind: OpKind,
    body: (op: Op) => Effect.Effect<void>,
  ) {
    const op: Op = { kind, fiber: undefined };
    ops.set(id, op);
    const fiber = yield* body(op).pipe(Effect.ensuring(done(id, op)), Effect.forkIn(runScope));
    op.fiber = fiber;
    return fiber;
  });

  /** One step: exit 0 ok, 10 refused, anything else (a timeout too) failed. Never fails. */
  /**
   * One step: exit 0 ok, 10 refused, anything else (a timeout too) failed. Never fails. Runs in
   * `cwd`: the worktree for claim and push, `stepsFolder` for renew and release.
   */
  const runStep = Effect.fn("PlaytestService.runStep")(function* (
    session: PlaytestSessionRecord,
    step: PlaytestStep,
    name: string,
    round: number,
    cwd: string,
  ) {
    const [program, ...args] = step.argv;
    // Windows runs a `.cmd` or `.bat` file only through a shell, which would read the arguments.
    const resolved = yield* resolveSpawnCommand(program!, args);
    if (resolved.shell) {
      return {
        result: "failed",
        detail: `Step ${name} names a program that needs a shell (a .cmd or .bat file); steps run without one.`,
        notStarted: true,
      } satisfies StepResult as StepResult;
    }
    const env = {
      ...stepBaseEnv(),
      [PLAYTEST_ENV.session]: envText(session.id),
      [PLAYTEST_ENV.target]: envText(session.target),
      [PLAYTEST_ENV.note]: envText(session.note),
      [PLAYTEST_ENV.project]: envText(session.projectId),
      [PLAYTEST_ENV.thread]: envText(session.threadId),
      [PLAYTEST_ENV.worktree]: envText(session.worktree),
      [PLAYTEST_ENV.step]: name,
      [PLAYTEST_ENV.round]: `${round}`,
    };
    return yield* runner
      .run({
        command: program!,
        args,
        cwd,
        env,
        timeout: Duration.seconds(step.timeoutSeconds),
        maxOutputBytes: STEP_MAX_OUTPUT_BYTES,
        outputMode: "tail",
        timeoutBehavior: "timedOutResult",
      })
      .pipe(
        Effect.map((output): StepResult => {
          if (output.timedOut) {
            return { result: "failed", detail: `Timed out after ${step.timeoutSeconds}s.` };
          }
          const code = output.code === null ? null : Number(output.code);
          const printed = tail([output.stdout.trim(), output.stderr.trim()].join("\n"));
          if (code === PLAYTEST_EXIT_OK) return { result: "ok", detail: printed };
          if (code === PLAYTEST_EXIT_REFUSED) return { result: "refused", detail: printed };
          const why = code === null ? "It was stopped by a signal." : `Exit code ${code}.`;
          return { result: "failed", detail: tail(`${why}\n${printed}`) };
        }),
        Effect.catch((error): Effect.Effect<StepResult> =>
          Effect.succeed({
            result: "failed",
            detail: runErrorText(error),
            ...(error._tag === "ProcessSpawnError" ? { notStarted: true as const } : {}),
          }),
        ),
      );
  });

  /**
   * Runs release and stores the answer: `ended`; still `ending` with the problem when release ran
   * and failed (the next end tries again); `ended` with `RELEASE_COULD_NOT_RUN` when it could not
   * even start, as no later end could run it either (the claim expires on the target by itself).
   */
  const releaseNow = Effect.fnUntraced(function* (id: string, op: Op) {
    const session = find(id);
    // Defensive: only release changes an `ending` session, and ends share one release.
    if (session === undefined || session.state !== "ending") return;
    yield* fs.makeDirectory(input.stepsFolder, { recursive: true }).pipe(Effect.ignore);
    const out = yield* runStep(
      session,
      session.adapter.release,
      "release",
      session.rounds,
      input.stepsFolder,
    );
    yield* locked(
      Effect.gen(function* () {
        const current = find(id);
        if (current === undefined || current.state !== "ending") return;
        const at = yield* now;
        // A release that works clears an earlier release's problem; a failed claim's stays.
        const { problem, ...rest } = current;
        const kept = problem?.startsWith(RELEASE_PROBLEM) ? {} : problem ? { problem } : {};
        if (out.notStarted) {
          yield* Effect.logWarning(
            `playtest: ${id} ends without a release, which could not run: ${out.detail}`,
          );
        }
        yield* record(
          replace(
            out.result === "ok"
              ? { ...rest, ...kept, state: "ended", endedAt: at }
              : out.notStarted
                ? {
                    ...current,
                    state: "ended",
                    endedAt: at,
                    problem: tail(`${RELEASE_COULD_NOT_RUN}${out.detail}`),
                  }
                : { ...current, problem: tail(`${RELEASE_PROBLEM}${out.detail}`) },
          ),
          id,
        );
        yield* done(id, op);
      }),
    ).pipe(Effect.uninterruptible);
  });

  const claimBody = (id: string) => (op: Op) =>
    Effect.gen(function* () {
      const session = find(id);
      if (session === undefined) return;
      const out = yield* runStep(session, session.adapter.claim, "claim", 0, session.worktree);
      const failed = yield* locked(
        Effect.gen(function* () {
          const current = find(id);
          // `end` took the session over while the claim ran. Reachable: the claim can return while
          // `end` holds the lock, and this block cannot be stopped once it waits (tested).
          if (current === undefined || current.state !== "claiming") return false;
          const at = yield* now;
          yield* record(
            replace(
              out.result === "ok"
                ? { ...current, state: "active", lastActivityAt: at }
                : out.result === "refused"
                  ? {
                      ...current,
                      state: "ended",
                      endReason: "claim-refused",
                      endedAt: at,
                      problem: tail(`claim: ${out.detail}`),
                    }
                  : // The claim may have been half taken: release puts the target back.
                    {
                      ...current,
                      state: "ending",
                      endReason: "claim-failed",
                      problem: tail(`claim: ${out.detail}`),
                    },
            ),
            id,
          );
          if (out.result === "failed") op.kind = "release";
          else yield* done(id, op);
          return out.result === "failed";
        }),
      ).pipe(Effect.uninterruptible);
      if (failed) yield* releaseNow(id, op);
    });

  const roundBody = (id: string, number: number) => (op: Op) =>
    Effect.gen(function* () {
      const steps = find(id)?.adapter.push ?? [];
      let last: StepResult = { result: "ok", detail: "" };
      let lastName = steps[0]?.name ?? "";
      for (const step of steps) {
        const session = yield* locked(
          Effect.gen(function* () {
            const current = find(id);
            // `end` stopped this round while the step before returned (reachable, tested).
            if (current?.state !== "active" || current.lastRound?.number !== number) return null;
            if (current.lastRound.step !== step.name) {
              yield* record(
                replace({ ...current, lastRound: { ...current.lastRound, step: step.name } }),
                id,
              );
            }
            return find(id) ?? null;
          }),
        ).pipe(Effect.uninterruptible);
        if (session === null) return;
        lastName = step.name;
        last = yield* runStep(session, step, step.name, number, session.worktree);
        if (last.result !== "ok") break;
      }
      yield* locked(
        Effect.gen(function* () {
          const current = find(id);
          // `end` stopped this round and recorded that, also when the last step returned while
          // `end` held the lock (reachable, tested).
          if (current?.state !== "active" || current.lastRound?.number !== number) return;
          const at = yield* now;
          yield* record(
            replace({
              ...current,
              lastActivityAt: at,
              lastRound: {
                ...current.lastRound,
                state: last.result === "ok" ? "done" : last.result,
                step: lastName,
                detail: last.detail,
                endedAt: at,
              },
            }),
            id,
          );
          yield* done(id, op);
        }),
      ).pipe(Effect.uninterruptible);
    });

  /** Waits for a step run; one that was interrupted (an end took over) is just over. */
  const settle = (fiber: Fiber.Fiber<void>) =>
    Fiber.await(fiber).pipe(
      Effect.flatMap((exit) =>
        Exit.isFailure(exit) && !Cause.hasInterruptsOnly(exit.cause)
          ? Effect.logError("playtest: a step run crashed", exit.cause)
          : Effect.void,
      ),
    );

  /** The thread's project, worktree and that project's adapter (null when off or incomplete). */
  const placeAndAdapter = Effect.fnUntraced(function* (threadId: ThreadId) {
    const settings = yield* readSettings;
    const place = yield* input.placeOf(threadId);
    if (place.type !== "ok") return yield* failure("thread_unavailable", place.message);
    const playtest = resolveProjectSettings(settings, place.projectId).settings.playtest;
    return { place, adapter: adapterOf(playtest) };
  });

  const start = Effect.fn("PlaytestService.start")(function* (request: {
    readonly threadId: ThreadId;
    readonly target: string;
    readonly note?: string | undefined;
  }) {
    const target = decodeTarget(request.target);
    if (target._tag === "None") {
      return yield* failure(
        "invalid",
        "The target is letters, digits, '.', '_' and '-', up to 64 characters.",
      );
    }
    const note = (request.note ?? "").replace(/\s+/gu, " ").trim();
    if (note.length > PLAYTEST_NOTE_MAX) {
      return yield* failure("invalid", `The note is at most ${PLAYTEST_NOTE_MAX} characters.`);
    }
    if (note.search(PLAYTEST_CONTROL_CHARACTERS) !== -1) {
      return yield* failure("invalid", "The note may not hold control characters.");
    }
    const { place, adapter } = yield* placeAndAdapter(request.threadId);
    if (adapter === null) {
      return yield* failure(
        "playtest_off",
        "Playtest is off for this project. Only a person can switch it on in the settings.",
      );
    }
    // Uninterruptible from the save to the started step: a cancelled caller leaves no half state.
    const begun = yield* locked(
      Effect.uninterruptible(
        Effect.gen(function* () {
          const all = yield* loaded;
          const live = liveOf(request.threadId);
          if (live !== undefined) {
            if (live.state === "active" && live.target === target.value) {
              return { type: "on", session: live } as const;
            }
            return yield* failure("busy", busyText(live, `start a session for ${target.value}`));
          }
          const at = yield* now;
          const session: PlaytestSessionRecord = {
            id: newSessionId(new Set(all.map((entry) => entry.id))),
            threadId: request.threadId,
            projectId: place.projectId,
            target: target.value,
            note,
            worktree: place.worktree,
            adapter,
            state: "claiming",
            startedAt: at,
            lastActivityAt: at,
            rounds: 0,
          };
          // Written before the claim runs: a claim cut by a stop is still released by an end.
          yield* save([...all, session], session.id);
          yield* Effect.logInfo(
            `playtest: ${session.id} claims ${session.target} for thread ${session.threadId}`,
          );
          return {
            type: "claim",
            id: session.id,
            fiber: yield* runOp(session.id, "claim", claimBody(session.id)),
          } as const;
        }),
      ),
    );
    if (begun.type === "on") {
      return {
        outcome: "already-on",
        session: viewOf(begun.session),
      } satisfies PlaytestStartResult;
    }
    yield* settle(begun.fiber);
    return yield* locked(
      Effect.map(found(begun.id), (session): PlaytestStartResult => {
        return {
          outcome:
            session.state === "active"
              ? "started"
              : session.endReason === "claim-refused"
                ? "refused"
                : session.endReason === "claim-failed"
                  ? "failed"
                  : "stopped",
          session: viewOf(session),
        };
      }),
    );
  });

  /** Why a session takes no new work now. */
  function busyText(session: PlaytestSessionRecord, wanted: string): string {
    const op = ops.get(session.id);
    if (session.state === "active" && op?.kind === "round") {
      return `Round ${session.lastRound?.number ?? session.rounds} runs (step ${session.lastRound?.step ?? "?"}); wait for it, or end the session.`;
    }
    if (session.state === "active") {
      return `This thread already playtests ${session.target} (${session.id}); end that session to ${wanted}.`;
    }
    if (session.state === "claiming") {
      return op === undefined
        ? `The claim of ${session.id} for ${session.target} did not finish (T3 stopped); end that session first, which releases it.`
        : `The claim of ${session.id} for ${session.target} runs; wait for it.`;
    }
    return op === undefined
      ? `${session.id} is ending, and its release has not worked yet; call end again.`
      : `${session.id} is ending; its release runs.`;
  }

  const push = Effect.fn("PlaytestService.push")(function* (request: {
    readonly threadId: ThreadId;
  }) {
    const { adapter } = yield* placeAndAdapter(request.threadId);
    // Uninterruptible from the save to the started step: a cancelled caller leaves no half state.
    const begun = yield* locked(
      Effect.uninterruptible(
        Effect.gen(function* () {
          yield* loaded;
          const session = liveOf(request.threadId);
          if (session === undefined) {
            return yield* failure("no_session", "This thread has no playtest session; start one.");
          }
          if (session.state !== "active" || ops.has(session.id)) {
            return yield* failure("busy", busyText(session, "push"));
          }
          // The session's own steps run; settings only decide whether rounds may run at all.
          if (adapter === null) {
            return yield* failure(
              "playtest_off",
              "Playtest was switched off for this project; end the session.",
            );
          }
          const number = session.rounds + 1;
          const at = yield* now;
          yield* save(
            replace({
              ...session,
              rounds: number,
              lastActivityAt: at,
              lastRound: {
                number,
                state: "running",
                step: session.adapter.push[0]!.name,
                detail: "",
                startedAt: at,
              },
            }),
            session.id,
          );
          return {
            id: session.id,
            fiber: yield* runOp(session.id, "round", roundBody(session.id, number)),
          };
        }),
      ),
    );
    yield* settle(begun.fiber);
    return yield* locked(
      Effect.map(found(begun.id), (session): PlaytestPushResult => {
        const state = session.lastRound?.state;
        return {
          outcome:
            state === "done" || state === "refused" || state === "failed" ? state : "stopped",
          session: viewOf(session),
        };
      }),
    );
  });

  const end = Effect.fn("PlaytestService.end")(function* (request: {
    readonly threadId: ThreadId;
    readonly reason: PlaytestEndReason;
  }) {
    // Uninterruptible from the save to the started step: a cancelled caller leaves no half state.
    const begun = yield* locked(
      Effect.uninterruptible(
        Effect.gen(function* () {
          const all = yield* loaded;
          const session = liveOf(request.threadId);
          if (session === undefined) {
            const last = [...all].reverse().find((entry) => entry.threadId === request.threadId);
            return { type: "none", session: last } as const;
          }
          const op = ops.get(session.id);
          // Two ends at once share one release.
          if (op?.kind === "release" && op.fiber !== undefined) {
            return { type: "release", id: session.id, fiber: op.fiber } as const;
          }
          const at = yield* now;
          const round = session.lastRound;
          yield* save(
            replace({
              ...session,
              state: "ending",
              endReason: session.endReason ?? request.reason,
              ...(round?.state === "running"
                ? {
                    lastRound: {
                      ...round,
                      state: "stopped",
                      detail: "The session ended while this round ran.",
                      endedAt: at,
                    },
                  }
                : {}),
            }),
            session.id,
          );
          yield* Effect.logInfo(
            `playtest: ${session.id} ends (${session.endReason ?? request.reason})`,
          );
          // The claim or round that runs is stopped first, then release puts the target back.
          const stopped = op?.fiber;
          ops.delete(session.id);
          const fiber = yield* runOp(session.id, "release", (release) =>
            (stopped === undefined ? Effect.void : Fiber.interrupt(stopped)).pipe(
              Effect.andThen(releaseNow(session.id, release)),
            ),
          );
          return { type: "release", id: session.id, fiber } as const;
        }),
      ),
    );
    if (begun.type === "none") {
      return {
        outcome: "nothing-to-end",
        session: begun.session === undefined ? null : viewOf(begun.session),
      } satisfies PlaytestEndResult;
    }
    yield* settle(begun.fiber);
    return yield* locked(
      Effect.map(found(begun.id), (session): PlaytestEndResult => {
        return {
          outcome: session.state === "ended" ? "ended" : "release-failed",
          session: viewOf(session),
        };
      }),
    );
  });

  /** The thread's session that is on, else its last one; null when it never had one. */
  const status = Effect.fn("PlaytestService.status")(function* (request: {
    readonly threadId: ThreadId;
  }) {
    return yield* locked(
      Effect.gen(function* () {
        const all = yield* loaded;
        const session =
          liveOf(request.threadId) ??
          [...all].reverse().find((entry) => entry.threadId === request.threadId);
        return session === undefined ? null : viewOf(session);
      }),
    );
  });

  return { filePath: store.filePath, start, push, end, status };
});

export type PlaytestService = Effect.Success<ReturnType<typeof make>>;
