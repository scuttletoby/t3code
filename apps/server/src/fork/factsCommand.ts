/**
 * Fork (placement row P4): runs `placement.factsCommand` on the machine whose settings name it, for
 * that machine's facts read (`environmentFacts.ts`). Threat notes: docs/fork/README.md,
 * `server.placement`; example scripts: fork-kit/placement-facts.
 */
import {
  canonicalPlacementLabels,
  PlacementLabel,
  placementFactsCommandProblem,
} from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { resolveSpawnCommand } from "@t3tools/shared/shell";
import * as Cache from "effect/Cache";
import * as Context from "effect/Context";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
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

/**
 * Whether PATHEXT, read as shared/shell's file lookup reads it (`resolveWindowsPathExtensions`: the
 * key in any case, entries trimmed and upper-cased, a dot added; unset or no entries means its
 * default list, which has .EXE), lists .EXE. Kept in step with it by the real-lookup tests.
 */
const pathextHasExe = (env: NodeJS.ProcessEnv): boolean => {
  const raw =
    env.PATHEXT ??
    Object.entries(env).find(
      ([key, value]) => key.toUpperCase() === "PATHEXT" && value !== undefined,
    )?.[1];
  const entries = (raw ?? "")
    .split(";")
    .map((entry) => entry.trim().toUpperCase())
    .filter((entry) => entry !== "");
  return entries.length === 0 || entries.some((entry) => entry === ".EXE" || entry === "EXE");
};

/** The first line of the program's output that says something, for the reason. */
const firstLine = (text: string) =>
  text
    .split(/\r?\n/u)
    .map((line) => line.trim())
    .find((line) => line !== "")
    ?.slice(0, 200);

/**
 * Exit 0: available, each printed line a label (others skipped); anything else: not available, with
 * the reason. Never fails. The path is checked again on this OS and run as one path, no shell, no
 * arguments; on Windows only an `.exe`, checked on the file the runner's own lookup would start, and
 * only on a server whose PATHEXT lists .EXE (else that lookup could start a different file).
 */
export const runFactsCommand = Effect.fn("FactsCommand.run")(function* (factsCommand: string) {
  if (factsCommand === "") return { available: true, labels: [] } satisfies FactsCommandResult;
  const platform = yield* HostProcess.Platform;
  const problem = placementFactsCommandProblem(factsCommand, platform);
  if (problem !== undefined) return notAvailable(problem);
  let command = factsCommand;
  if (platform === "win32") {
    if (!/\.exe$/iu.test(factsCommand)) {
      return notAvailable(
        `factsCommand "${factsCommand}" is not an .exe: on Windows a .cmd, .bat or script needs a shell, which placement never uses`,
      );
    }
    // Without .EXE in PATHEXT the lookup tries `facts.exe` plus each PATHEXT ending, and the runner
    // looks up again before it starts the program: a `facts.exe.CMD` planted between the two would
    // run through a shell. With .EXE listed, both lookups try only `.exe` spellings of the path.
    if (!pathextHasExe(yield* HostProcess.Environment)) {
      return notAvailable(
        `factsCommand "${factsCommand}" cannot run on this server: its PATHEXT has no .EXE, so the file lookup would also start a planted "${factsCommand}.CMD" (or .BAT) through a shell, which placement never uses`,
      );
    }
    const resolved = yield* resolveSpawnCommand(factsCommand, []);
    if (resolved.shell) {
      return notAvailable(
        `factsCommand "${factsCommand}" would run through a shell on this server (as ${resolved.command}), which placement never uses`,
      );
    }
    if (!/\.exe$/iu.test(resolved.command)) {
      return notAvailable(
        `factsCommand "${factsCommand}" runs as "${resolved.command}" on this server, not an .exe`,
      );
    }
    command = resolved.command;
  }
  const runner = yield* ProcessRunner.ProcessRunner;
  const result = yield* runner
    .run({
      command,
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
    // The error output first: a label line printed before the failure would hide it.
    const said = firstLine(`${stderr}\n${stdout}`);
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

/** One answer serves every facts read in this window: senders ask each machine per placed task. */
export const FACTS_COMMAND_CACHE_TTL = Duration.seconds(15);

/**
 * This server's `factsCommand` runs: one run per command at a time, its answer kept for
 * {@link FACTS_COMMAND_CACHE_TTL}. A run every reader stops waiting for is stopped and kept by none.
 */
export class FactsCommands extends Context.Service<
  FactsCommands,
  { readonly run: (factsCommand: string) => Effect.Effect<FactsCommandResult> }
>()("t3/fork/factsCommand/FactsCommands") {}

/** Its span shows a boot built it (holdWork.boot.test.ts); without it every read says "no process runner". */
export const make = Effect.gen(function* () {
  const runner = yield* ProcessRunner.ProcessRunner;
  const cache = yield* Cache.make({
    capacity: 4,
    timeToLive: FACTS_COMMAND_CACHE_TTL,
    lookup: (factsCommand: string) =>
      runFactsCommand(factsCommand).pipe(
        Effect.provideService(ProcessRunner.ProcessRunner, runner),
      ),
  });
  return FactsCommands.of({
    run: Effect.fn("FactsCommands.run")((factsCommand: string) => Cache.get(cache, factsCommand)),
  });
}).pipe(Effect.withSpan("FactsCommands.make"));

/** Needs a process spawner; in `forkLayers.layerRuntimeServices`. */
export const layer = Layer.effect(FactsCommands, make).pipe(Layer.provide(ProcessRunner.layer));
