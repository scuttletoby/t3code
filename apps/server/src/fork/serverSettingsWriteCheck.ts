/**
 * Fork: the server's save check for fork settings. A refused save fails with upstream's
 * `ServerSettingsError` (operation `normalize`), its cause a `ForkSettingsWriteError` with the fix.
 * The settings file calls `check` in its write path and `withCheck` in its test layer.
 */
import { type ServerSettings, ServerSettingsError } from "@t3tools/contracts";
import {
  ForkSettingsWriteError,
  forkSettingsWriteProblem,
  playtestProgramProblem,
} from "@t3tools/shared/forkSettingsPatch";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { Effect } from "effect";

/** Fails when saving `next` over `current` would bring in a fork setting that cannot apply. */
export function check(
  current: ServerSettings,
  next: ServerSettings,
  settingsPath: string,
): Effect.Effect<void, ServerSettingsError> {
  return Effect.flatMap(HostProcess.Platform, (platform) => {
    const problem =
      forkSettingsWriteProblem(current, next) ?? playtestProgramProblem(current, next, platform);
    return problem === undefined
      ? Effect.void
      : Effect.fail(
          new ServerSettingsError({
            settingsPath,
            operation: "normalize",
            cause: new ForkSettingsWriteError({ problem }),
          }),
        );
  });
}

/** `update`, then {@link check} on what it returns. */
export const withCheck =
  <E>(
    update: (current: ServerSettings) => Effect.Effect<ServerSettings, E>,
    settingsPath: string,
  ) =>
  (current: ServerSettings): Effect.Effect<ServerSettings, E | ServerSettingsError> =>
    update(current).pipe(Effect.tap((next) => check(current, next, settingsPath)));
