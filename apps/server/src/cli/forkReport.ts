import * as Console from "effect/Console";
import * as Effect from "effect/Effect";
import * as References from "effect/References";
import { Command, Flag } from "effect/cli";

import { formatActivityReading, readActivityProbe } from "../golive/activityProbeRead.ts";
import { formatForkReport, readForkReport } from "../golive/forkReport.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";

/**
 * Fork (go-live kit): prints what a named T3 home holds as JSON, writing nothing; exit 1 when `ok` is
 * false. The managed policy is read as a start reads it. `--activity` instead asks the home's running
 * server whether it is busy: exit 0 only when it says quiet.
 */
export const forkReportCommand = Command.make("__fork-report", {
  baseDir: Flag.String("base-dir").pipe(
    Flag.withDescription("The T3 home to read (its state is under userdata)."),
  ),
  activity: Flag.Boolean("activity").pipe(
    Flag.withDescription(
      "Instead: ask the server running in the home whether it is busy (server.activityProbe). One JSON line; exit 0 only when quiet.",
    ),
    Flag.withDefault(false),
  ),
}).pipe(
  Command.unlisted,
  Command.withHandler(({ baseDir, activity }) =>
    Effect.gen(function* () {
      if (activity) {
        const reading = yield* Effect.promise(() => readActivityProbe(baseDir));
        yield* Console.log(formatActivityReading(reading));
        if (!reading.quiet) {
          process.exitCode = 1;
        }
        return;
      }
      const policy = yield* ManagedPolicy.ManagedPolicy.pipe(
        Effect.provide(ManagedPolicy.layer),
        // stdout carries only the JSON; a policy that fails to load is in the report's problems.
        Effect.provideService(References.MinimumLogLevel, "None"),
        Effect.map((loaded) => loaded.settings),
        Effect.catch((error) => Effect.succeed({ unreadable: String(error.message) })),
      );
      const report = readForkReport(baseDir, policy);
      yield* Console.log(formatForkReport(report));
      if (!report.ok) {
        process.exitCode = 1;
      }
    }),
  ),
);
