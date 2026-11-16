/**
 * Fork: the server's save check for fork settings (`forkSettingsWriteProblem`). A save that could
 * not apply fails with upstream's `ServerSettingsError` (operation `normalize`, as for a patch
 * that does not decode); its cause, a `ForkSettingsWriteError`, says why and how to fix it. The
 * server's settings file calls `check` in its write path and `withCheck` in its test layer.
 */
import { type ServerSettings, ServerSettingsError } from "@t3tools/contracts";
import {
  ForkSettingsWriteError,
  forkSettingsWriteProblem,
} from "@t3tools/shared/forkSettingsPatch";
import { Effect } from "effect";

/** Fails when saving `next` over `current` would bring in a fork setting that cannot apply. */
export function check(
  current: ServerSettings,
  next: ServerSettings,
  settingsPath: string,
): Effect.Effect<void, ServerSettingsError> {
  const problem = forkSettingsWriteProblem(current, next);
  return problem === undefined
    ? Effect.void
    : Effect.fail(
        new ServerSettingsError({
          settingsPath,
          operation: "normalize",
          cause: new ForkSettingsWriteError({ problem }),
        }),
      );
}

/** `update`, then {@link check} on what it returns. */
export const withCheck =
  <E>(
    update: (current: ServerSettings) => Effect.Effect<ServerSettings, E>,
    settingsPath: string,
  ) =>
  (current: ServerSettings): Effect.Effect<ServerSettings, E | ServerSettingsError> =>
    update(current).pipe(Effect.tap((next) => check(current, next, settingsPath)));
