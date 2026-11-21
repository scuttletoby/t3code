/**
 * Fork: the server's save check for fork settings. A refused save fails with upstream's
 * `ServerSettingsError` (operation `normalize`), its cause a `ForkSettingsWriteError` with the fix.
 * The settings file calls `check` in its write path, `withCheck` in its test layer and `forThisOs`
 * in its load.
 */
import { type ServerSettings, ServerSettingsError, placementOnPlatform } from "@t3tools/contracts";
import {
  ForkSettingsWriteError,
  forkSettingsWriteProblem,
  placementFactsCommandSaveProblem,
  playtestProgramProblem,
} from "@t3tools/shared/forkSettingsPatch";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { Effect } from "effect";

/** Fails when saving `next` over `current` would bring in a fork setting or interval that cannot apply. */
export function check(
  current: ServerSettings,
  next: ServerSettings,
  settingsPath: string,
): Effect.Effect<void, ServerSettingsError> {
  return Effect.flatMap(HostProcess.Platform, (platform) => {
    const problem =
      forkSettingsWriteProblem(current, next) ??
      playtestProgramProblem(current, next, platform) ??
      placementFactsCommandSaveProblem(current, next, platform);
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

/**
 * The settings as this machine reads them: a `placement.factsCommand` this OS cannot run reads as
 * off, kept as written with its problem (contracts' `placementOnPlatform`). The load applies it.
 */
export const forThisOs = (settings: ServerSettings): Effect.Effect<ServerSettings> =>
  Effect.map(HostProcess.Platform, (platform) => {
    const placement = placementOnPlatform(settings.placement, platform);
    return placement === settings.placement ? settings : { ...settings, placement };
  });

/** `update`, then {@link check} on what it returns. */
export const withCheck =
  <E>(
    update: (current: ServerSettings) => Effect.Effect<ServerSettings, E>,
    settingsPath: string,
  ) =>
  (current: ServerSettings): Effect.Effect<ServerSettings, E | ServerSettingsError> =>
    update(current).pipe(Effect.tap((next) => check(current, next, settingsPath)));
