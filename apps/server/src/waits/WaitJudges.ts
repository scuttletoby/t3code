// @effect-diagnostics nodeBuiltinImport:off -- the file check process is an external runtime boundary.
/**
 * Fork (waits): the judges, one per kind, that read a condition once. A judge never fails: an error
 * is a `failed` judgement, and the wait service decides what it means. Here: the `file` judge (in
 * the server and in a short-lived process), the `check` judge (a command from settings) and the
 * `unit` judge (one `systemctl show`). The `pr` judge comes with its own PR.
 */
import * as NodeChildProcess from "node:child_process";
import * as NodeFS from "node:fs";
import type { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitCheck, WaitKind, WaitTarget } from "@t3tools/contracts/waitFor";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import { resolveSpawnCommand } from "@t3tools/shared/shell";
import { judgeExit, type Judgement, messageText, validateWaitSpec } from "@t3tools/shared/waitFor";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import type * as PlatformError from "effect/PlatformError";
import * as Semaphore from "effect/Semaphore";
import type { ServerSettings } from "@t3tools/contracts";

import * as ProcessRunner from "../processRunner.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import { ARG_PATTERN_LIMIT_MS, testArgPattern } from "./argPattern.ts";
import { WAIT_STAT_PATH_ENV, WAIT_STAT_SCRIPT } from "./waitStatChild.ts";

export interface WaitJudgeContext {
  /** The stored wait, or null when the condition is read once at registration. */
  readonly waitId: string | null;
  readonly now: number;
  /** The thread the wait belongs to, and the project it was registered in. */
  readonly threadId: ThreadId;
  readonly projectId: ProjectId;
  /**
   * Check waits: where the check runs, worked out by the wait service, which times the run by the
   * same project's check. Without it the check judge works it out itself.
   */
  readonly place?: CheckPlace | undefined;
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

/** The most a check or `systemctl` may print on each of stdout and stderr; more is an error. */
const CHECK_MAX_OUTPUT_BYTES = 8192;
/** A `systemctl show` that has not answered by then is killed. */
const UNIT_LIMIT_MS = 5_000;

/**
 * Where a check runs: the project the wait's thread is in now (it follows a thread that moved),
 * and that project's main workspace root as the working directory.
 */
export type CheckPlace =
  | { readonly type: "ok"; readonly projectId: ProjectId; readonly cwd: string }
  /** The project is gone, so the check can never run again. */
  | { readonly type: "gone"; readonly message: string }
  /** It cannot be read now; tried again next time. */
  | { readonly type: "unreadable"; readonly message: string };

const transient = (message: string): Judgement => ({ type: "failed", message, permanent: false });
const permanent = (message: string): Judgement => ({ type: "failed", message, permanent: true });

/** A system error code (`ENOENT`, `EACCES`, ...) somewhere in a spawn error's causes. */
function causeCode(error: unknown): string | undefined {
  let current: unknown = error;
  for (let depth = 0; depth < 4 && typeof current === "object" && current !== null; depth += 1) {
    const code = (current as { readonly code?: unknown }).code;
    if (typeof code === "string" && /^[A-Z0-9_]{1,40}$/u.test(code)) return code;
    const reason = (current as { readonly reason?: unknown }).reason;
    current =
      typeof reason === "object" && reason !== null && "cause" in reason
        ? (reason as { readonly cause: unknown }).cause
        : (current as { readonly cause?: unknown }).cause;
  }
  return undefined;
}

/** Why a process could not be run, without its command line (the record already names it). */
function runErrorText(error: ProcessRunner.ProcessRunError): string {
  switch (error._tag) {
    case "ProcessOutputLimitError":
      return "Printed too much output.";
    case "ProcessTimeoutError":
      return "Timed out.";
    case "ProcessSpawnError": {
      const code = causeCode(error.cause);
      return `It could not start${code === undefined ? "" : ` (${code})`}.`;
    }
    case "ProcessReadError":
    case "ProcessStdinError":
      return `Its output could not be read (${error._tag}).`;
  }
}

/** A check as it runs now, or the answer of a wait that it can no longer run. */
export type CheckLookup =
  | { readonly ok: true; readonly check: WaitCheck }
  | { readonly ok: false; readonly judgement: Judgement };

/**
 * The check a stored wait names, in `projectId`'s settings, with the wait's arguments tested again
 * (the pattern in a worker, see `argPattern.ts`). Removed or narrowed: a permanent error, so the
 * wait ends and nothing more runs. A pattern too slow to answer: a transient error.
 */
export const lookUpCheck: (
  spec: Extract<WaitTarget, { readonly kind: "check" }>["spec"],
  projectId: ProjectId,
  settings: ServerSettings,
  platform: NodeJS.Platform,
) => Effect.Effect<CheckLookup> = Effect.fn("WaitJudges.lookUpCheck")(function* (
  spec: Extract<WaitTarget, { readonly kind: "check" }>["spec"],
  projectId: ProjectId,
  settings: ServerSettings,
  platform: NodeJS.Platform,
) {
  const checks = resolveProjectSettings(settings, projectId).settings.waitChecks;
  const valid = validateWaitSpec({
    input: { kind: "check", check: spec },
    checks,
    platform,
    accounts: new Set(),
    testArgPattern: false,
  });
  if (!valid.ok) {
    return {
      ok: false,
      judgement: permanent(
        valid.code === "unknown_check"
          ? `Check "${spec.name}" is no longer in this project's settings.`
          : `Check "${spec.name}" was changed in settings and no longer allows this wait: ${valid.message}`,
      ),
    } as const;
  }
  const check = checks[spec.name]!;
  const tested = yield* testArgPattern(check.argPattern, spec.args);
  if (tested.type === "slow" || tested.type === "failed") {
    return {
      ok: false,
      judgement: transient(
        tested.type === "failed"
          ? `Check "${spec.name}": ${tested.message}`
          : `The argument pattern of check "${spec.name}" gave no answer in ${ARG_PATTERN_LIMIT_MS / 1000}s.`,
      ),
    } as const;
  }
  if (tested.type === "bad") {
    return {
      ok: false,
      judgement: permanent(
        // @effect-diagnostics-next-line globalJsonInEffect:off - quotes the argument for the message.
        `Check "${spec.name}" was changed in settings and no longer allows this wait: argument ${JSON.stringify(messageText(tested.arg, 100))} does not match its pattern.`,
      ),
    } as const;
  }
  return { ok: true, check } as const;
});

/**
 * On Linux a check runs under `timeout`, so it also ends at its own time limit when the server that
 * started it is gone (a crash, or a kill that skips the server's clean-up); otherwise the server is
 * what stops it. It gets this much more time than the server's own limit, so the server's limit
 * normally comes first.
 */
const CHECK_ORPHAN_EXTRA_SECONDS = 5;
const TIMEOUT_PROGRAMS: ReadonlyArray<string> = ["/usr/bin/timeout", "/bin/timeout"];
/** `timeout` options that GNU coreutils and BusyBox (1.36 and later) both take. */
const timeoutOptions = (seconds: number) => ["-k", "2", `${seconds}`];
/** The exit code the `timeout` test run asks for, so a usage error (exit 1) cannot pass it. */
const TIMEOUT_TEST_EXIT = 42;
const TIMEOUT_TEST_LIMIT_MS = 5_000;
/** How long after a test run that hung `timeout` is tested again; checks run without it meanwhile. */
const TIMEOUT_TEST_RETRY_MS = 10 * 60_000;

/** The command line a check runs as: under `timeout` when there is one (Linux), else as it is. */
export function checkCommandLine(
  check: WaitCheck,
  args: ReadonlyArray<string>,
  timeoutProgram: string | null,
): { readonly command: string; readonly args: ReadonlyArray<string> } {
  const [command, ...fixed] = check.argv;
  const all = [...fixed, ...args];
  if (timeoutProgram === null) return { command: command!, args: all };
  return {
    command: timeoutProgram,
    args: [...timeoutOptions(check.timeoutSeconds + CHECK_ORPHAN_EXTRA_SECONDS), command!, ...all],
  };
}

/** How a `timeout` program answered its test run; `hung` when the test run itself did not end. */
type TimeoutProgramTest = "works" | "refused" | "hung";

/**
 * Whether `program` runs a command with the options `checkCommandLine` gives and passes its exit
 * code back. An older BusyBox `timeout` refuses `-k` with exit 1, which a check would read as "not
 * yet" for ever, so such a program is not used. A test run that is killed at its own limit (a very
 * busy host) says nothing either way.
 */
export const testTimeoutProgram = (
  program: string,
  limitMs: number = TIMEOUT_TEST_LIMIT_MS,
): Effect.Effect<TimeoutProgramTest> =>
  Effect.callback<TimeoutProgramTest>((resume) => {
    let child: NodeChildProcess.ChildProcess;
    try {
      child = NodeChildProcess.execFile(
        program,
        [...timeoutOptions(10), "/bin/sh", "-c", `exit ${TIMEOUT_TEST_EXIT}`],
        { timeout: limitMs, killSignal: "SIGKILL", windowsHide: true },
        (error) => {
          const failure = error as {
            readonly code?: unknown;
            readonly killed?: unknown;
            readonly signal?: unknown;
          } | null;
          resume(
            Effect.succeed(
              failure?.code === TIMEOUT_TEST_EXIT
                ? "works"
                : failure?.killed === true && failure.signal === "SIGKILL"
                  ? "hung"
                  : "refused",
            ),
          );
        },
      );
    } catch {
      resume(Effect.succeed("refused"));
      return;
    }
    return Effect.sync(() => {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    });
  });

/**
 * The first of `programs` that exists and works as `testTimeoutProgram` tests it, else null.
 * `known` is false when a program before the answer hung in its test, so a later look may differ.
 */
export const findTimeoutProgram = Effect.fnUntraced(function* (
  programs: ReadonlyArray<string>,
  timing: { readonly limitMs?: number; readonly retryMs?: number } = {},
) {
  const limitMs = timing.limitMs ?? TIMEOUT_TEST_LIMIT_MS;
  const retryMs = timing.retryMs ?? TIMEOUT_TEST_RETRY_MS;
  let known = true;
  for (const program of programs) {
    if (!NodeFS.existsSync(program)) continue;
    const test = yield* testTimeoutProgram(program, limitMs);
    if (test === "works") return { program, known };
    if (test === "hung") {
      known = false;
      yield* Effect.logWarning(
        `waits: ${program} did not answer its test run in ${Duration.format(Duration.millis(limitMs))}, so checks run without it for now; it is tested again in ${Duration.format(Duration.millis(retryMs))}.`,
      );
      continue;
    }
    yield* Effect.logWarning(
      `waits: ${program} does not take "${timeoutOptions(1).slice(0, 2).join(" ")}", so checks run without it; a check left behind by a server crash runs until it ends by itself.`,
    );
  }
  return { program: null, known };
});

/**
 * Runs a named check from settings without a shell, in the project's main workspace root, with a
 * timeout and an output cap. The check is looked up again on every run in the thread's current
 * project; a removed or narrowed check is a permanent error. A wait brings no command, cwd or env.
 * Test options: `timeoutProgram`, `timeoutPrograms` (tested on Linux) and `timeoutTest` timing.
 */
export const makeCheckJudge = Effect.fn("WaitJudges.makeCheckJudge")(function* (input: {
  readonly placeOf: (context: WaitJudgeContext) => Effect.Effect<CheckPlace>;
  readonly timeoutProgram?: string | null;
  readonly timeoutPrograms?: ReadonlyArray<string>;
  readonly timeoutTest?: { readonly limitMs?: number; readonly retryMs?: number };
}) {
  const timing = input.timeoutTest ?? {};
  const settingsService = yield* ServerSettingsService;
  const runner = yield* ProcessRunner.ProcessRunner;
  const platform = yield* HostProcess.Platform;
  // Looked for and tested at the first check, and kept once the answer is known. A test run that
  // hung says nothing, so it is tested again at a later check; until then checks run without it.
  let found: { readonly program: string | null } | null =
    input.timeoutProgram !== undefined
      ? { program: input.timeoutProgram }
      : platform !== "linux"
        ? { program: null }
        : null;
  let testAgainAt = 0;
  const finding = yield* Semaphore.make(1);
  const timeoutProgram = finding.withPermits(1)(
    Effect.gen(function* () {
      if (found !== null) return found.program;
      const now = yield* Clock.currentTimeMillis;
      if (now < testAgainAt) return null;
      const result = yield* findTimeoutProgram(input.timeoutPrograms ?? TIMEOUT_PROGRAMS, timing);
      if (result.known || result.program !== null) found = { program: result.program };
      else testAgainAt = now + (timing.retryMs ?? TIMEOUT_TEST_RETRY_MS);
      return result.program;
    }),
  );
  const judge: WaitJudge<"check"> = Effect.fn("WaitJudges.check")(function* ({ spec }, context) {
    const settings = yield* settingsService.getSettings.pipe(Effect.option);
    if (settings._tag === "None") return transient("The settings cannot be read.");
    const place = context.place ?? (yield* input.placeOf(context));
    if (place.type === "gone") return permanent(place.message);
    if (place.type === "unreadable") return transient(place.message);
    const found = yield* lookUpCheck(spec, place.projectId, settings.value, platform);
    if (!found.ok) return found.judgement;
    const check = found.check;
    // Windows runs a `.cmd` or `.bat` file only through a shell, which would read the arguments.
    const resolved = yield* resolveSpawnCommand(check.argv[0]!, [
      ...check.argv.slice(1),
      ...spec.args,
    ]);
    if (resolved.shell) {
      return permanent(
        `Check "${spec.name}" names a program that needs a shell (a .cmd or .bat file); checks run without one.`,
      );
    }
    const line = checkCommandLine(check, spec.args, yield* timeoutProgram);
    return yield* runner
      .run({
        command: line.command,
        args: [...line.args],
        cwd: place.cwd,
        timeout: Duration.seconds(check.timeoutSeconds),
        maxOutputBytes: CHECK_MAX_OUTPUT_BYTES,
        outputMode: "error",
        timeoutBehavior: "timedOutResult",
      })
      .pipe(
        Effect.map((result) =>
          judgeExit({
            exitCode: result.code === null ? null : Number(result.code),
            stdout: result.stdout,
            stderr: result.stderr,
            timedOut: result.timedOut,
          }),
        ),
        Effect.catch((error) => Effect.succeed(transient(runErrorText(error)))),
      );
  });
  return judge;
});

/** `systemctl show` lines (`Key=Value`) as a map. */
function parseShow(stdout: string): ReadonlyMap<string, string> {
  const values = new Map<string, string>();
  for (const line of stdout.split(/\r?\n/u)) {
    const at = line.indexOf("=");
    if (at > 0) values.set(line.slice(0, at), line.slice(at + 1).trim());
  }
  return values;
}

const UNIT_NOT_AVAILABLE =
  "Unit waits need systemd on Linux, and it is not available on this server. Use a check command.";

/**
 * Met (`ended`) when `systemctl [--user] show` says the unit is `inactive` or `failed`. A unit
 * systemd does not know counts as ended: it forgets a transient unit once it has ended. Off Linux,
 * without `systemctl`, or on a system not booted with systemd, every answer is the permanent
 * error "not available", never met. `command` is for tests.
 */
export const makeUnitJudge = Effect.fn("WaitJudges.makeUnitJudge")(function* (options?: {
  readonly command?: string;
}) {
  const runner = yield* ProcessRunner.ProcessRunner;
  const platform = yield* HostProcess.Platform;
  const command = options?.command ?? "systemctl";
  const judge: WaitJudge<"unit"> = ({ spec }) => {
    if (platform !== "linux") return Effect.succeed(permanent(UNIT_NOT_AVAILABLE));
    const args = [
      ...(spec.scope === "user" ? ["--user"] : []),
      "show",
      "--property=ActiveState,LoadState",
      "--",
      spec.name,
    ];
    return runner
      .run({
        command,
        args,
        timeout: Duration.millis(UNIT_LIMIT_MS),
        maxOutputBytes: CHECK_MAX_OUTPUT_BYTES,
        outputMode: "error",
        timeoutBehavior: "timedOutResult",
      })
      .pipe(
        Effect.map((result): Judgement => {
          if (result.timedOut) return transient("systemctl timed out.");
          if (/not been booted with systemd|System has not been booted/iu.test(result.stderr)) {
            return permanent(UNIT_NOT_AVAILABLE);
          }
          const values = parseShow(result.stdout);
          const active = values.get("ActiveState");
          if (Number(result.code) !== 0 || active === undefined || active === "") {
            const line = result.stderr.split(/\r?\n/u).find((text) => text.trim() !== "") ?? "";
            const reason = messageText(line, 200).trim();
            return transient(
              `systemctl could not read the unit (exit ${result.code ?? "none"})${reason === "" ? "." : `: ${reason}`}`,
            );
          }
          const unknown = values.get("LoadState") === "not-found";
          if (active === "inactive" || active === "failed") {
            return {
              type: "met",
              outcome: "ended",
              detail: unknown
                ? "systemd does not know this unit (never started, or ended and forgotten)."
                : `It is ${active}.`,
            };
          }
          return { type: "notYet", detail: `It is ${messageText(active, 40)}.` };
        }),
        Effect.catch((error) =>
          Effect.succeed(
            error._tag === "ProcessSpawnError" && causeCode(error.cause) === "ENOENT"
              ? permanent(UNIT_NOT_AVAILABLE)
              : transient(`systemctl: ${runErrorText(error)}`),
          ),
        ),
      );
  };
  return judge;
});
