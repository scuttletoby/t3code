/**
 * Fork (placement row P4, the runner's checks): runs `placement.factsCommand` on the
 * machine whose settings name it. P4 wires it into this machine's facts read and adds the example
 * scripts; nothing calls it before that. Threat notes: docs/fork/README.md, `server.placement`.
 */
import {
  canonicalPlacementLabels,
  PlacementLabel,
  placementFactsCommandProblem,
} from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import * as ProcessRunner from "../processRunner.ts";

/** Longer than this and the machine is "not available" (the program is stopped). */
export const FACTS_COMMAND_TIMEOUT = Duration.seconds(5);
/** Output past this is cut off; labels come only from what was kept. */
const FACTS_COMMAND_MAX_OUTPUT_BYTES = 16 * 1024;
/** At most this many labels from one run, as for the setting's own list. */
const FACTS_COMMAND_LABELS_MAX = 64;

export type FactsCommandResult =
  | { readonly available: true; readonly labels: ReadonlyArray<string> }
  | { readonly available: false; readonly reason: string };

const notAvailable = (reason: string): FactsCommandResult => ({ available: false, reason });

const isLabel = Schema.is(PlacementLabel);

/** The first line of the program's output that says something, for the reason. */
const firstLine = (text: string) =>
  text
    .split(/\r?\n/u)
    .map((line) => line.trim())
    .find((line) => line !== "")
    ?.slice(0, 200);

/**
 * Exit 0: available, each printed line one more label (a line that is not one label is skipped).
 * Any other exit, a timeout or a failed start: not available, with the reason. Never fails. The path
 * is checked again on this OS (a hand edit skips the save's check) and run as one path, no shell,
 * no arguments; on Windows only an `.exe`, since a `.cmd` or `.bat` needs a shell.
 */
export const runFactsCommand = Effect.fn("FactsCommand.run")(function* (factsCommand: string) {
  if (factsCommand === "") return { available: true, labels: [] } satisfies FactsCommandResult;
  const platform = yield* HostProcess.Platform;
  const problem = placementFactsCommandProblem(factsCommand, platform);
  if (problem !== undefined) return notAvailable(problem);
  if (platform === "win32" && !/\.exe$/iu.test(factsCommand)) {
    return notAvailable(
      `factsCommand "${factsCommand}" is not an .exe: on Windows a .cmd, .bat or script needs a shell, which placement never uses`,
    );
  }
  const runner = yield* ProcessRunner.ProcessRunner;
  const result = yield* runner
    .run({
      command: factsCommand,
      args: [],
      timeout: FACTS_COMMAND_TIMEOUT,
      timeoutBehavior: "timedOutResult",
      maxOutputBytes: FACTS_COMMAND_MAX_OUTPUT_BYTES,
      outputMode: "truncate",
    })
    .pipe(Effect.result);
  if (result._tag === "Failure") {
    const failure = result.failure;
    // A program a signal ends (the OOM killer, a kill) has no exit code to read.
    return failure._tag === "ProcessReadError" && failure.stream === "exitCode"
      ? notAvailable("factsCommand was stopped by a signal")
      : notAvailable(`factsCommand could not run (${failure._tag})`);
  }
  const { code, stdout, stderr, stdoutTruncated, timedOut } = result.success;
  if (timedOut) {
    return notAvailable(
      `factsCommand did not answer within ${Duration.toSeconds(FACTS_COMMAND_TIMEOUT)} seconds`,
    );
  }
  if (code !== 0) {
    const said = firstLine(`${stdout}\n${stderr}`);
    return notAvailable(
      `factsCommand exited with ${code === null ? "no code" : `code ${code}`}${said === undefined ? "" : `: ${said}`}`,
    );
  }
  const lines = stdout.split(/\r?\n/u);
  // Output cut at the limit: its last line may be part of a label ("gpu" of "gpu-large").
  if (stdoutTruncated) lines.pop();
  const labels = canonicalPlacementLabels(lines).filter(isLabel);
  return {
    available: true,
    labels: labels.slice(0, FACTS_COMMAND_LABELS_MAX),
  } satisfies FactsCommandResult;
});
