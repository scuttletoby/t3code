import * as Console from "effect/Console";
import * as Effect from "effect/Effect";
import * as References from "effect/References";
import { Command, Flag } from "effect/cli";

import { formatForkReport, readForkReport } from "../golive/forkReport.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";

/**
 * Fork (go-live kit): prints what a T3 home holds as JSON, writing nothing. The home must be named:
 * a report on a guessed home could pass a check against the wrong data. Exit 1 when `ok` is false.
 * The machine's managed policy is read as a start reads it, so a switch it turns on shows as on.
 */
export const forkReportCommand = Command.make("__fork-report", {
  baseDir: Flag.String("base-dir").pipe(
    Flag.withDescription("The T3 home to read (its state is under userdata)."),
  ),
}).pipe(
  Command.unlisted,
  Command.withHandler(({ baseDir }) =>
    Effect.gen(function* () {
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
