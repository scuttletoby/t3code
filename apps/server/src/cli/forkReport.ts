import * as Console from "effect/Console";
import * as Effect from "effect/Effect";
import { Command, Flag } from "effect/cli";

import { formatForkReport, readForkReport } from "../golive/forkReport.ts";

/**
 * Fork (go-live kit): prints what a T3 home holds as JSON, writing nothing. The home must be named:
 * a report on a guessed home could pass a check against the wrong data. Exit 1 when `ok` is false.
 */
export const forkReportCommand = Command.make("__fork-report", {
  baseDir: Flag.String("base-dir").pipe(
    Flag.withDescription("The T3 home to read (its state is under userdata)."),
  ),
}).pipe(
  Command.unlisted,
  Command.withHandler(({ baseDir }) =>
    Effect.gen(function* () {
      const report = readForkReport(baseDir);
      yield* Console.log(formatForkReport(report));
      if (!report.ok) {
        process.exitCode = 1;
      }
    }),
  ),
);
