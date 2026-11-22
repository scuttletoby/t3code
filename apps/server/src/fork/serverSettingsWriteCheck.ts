/**
 * Fork: the server's save check for fork settings. A refused save fails with upstream's
 * `ServerSettingsError` (operation `normalize`), its cause a `ForkSettingsWriteError` with the fix.
 * The settings file calls `check` in its write path and `withCheck` in its test layer.
 */
import { type ServerSettings, ServerSettingsError } from "@t3tools/contracts";
import {
  ForkSettingsWriteError,
  forkSettingsWriteProblem,
  placementFactsCommandSaveProblem,
  playtestProgramProblem,
} from "@t3tools/shared/forkSettingsPatch";
import * as NodeServices from "@effect/platform-node/NodeServices";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { Effect } from "effect";

import { handoffCommandSaveProblem } from "./handoff/handoffCommandSaveCheck.ts";
import { threadRolesSaveProblem } from "./threadRoleTargets.ts";

/** Fails when saving `next` over `current` would bring in a fork setting or interval that cannot apply. */
export const check = Effect.fn("ForkSettingsWrite.check")(function* (
  current: ServerSettings,
  next: ServerSettings,
  settingsPath: string,
) {
  const platform = yield* HostProcess.Platform;
  const problem =
    forkSettingsWriteProblem(current, next) ??
    playtestProgramProblem(current, next, platform) ??
    placementFactsCommandSaveProblem(current, next, platform) ??
    // Reads the files a new handoff command names (fork/handoff/handoffCommandSaveCheck.ts).
    (yield* handoffCommandSaveProblem(current, next, platform).pipe(
      Effect.provide(NodeServices.layer),
    )) ??
    // Reads the threads a new role pointer names, so it runs last (fork/threadRoleTargets.ts).
    (yield* threadRolesSaveProblem(current, next));
  if (problem === undefined) return;
  return yield* new ServerSettingsError({
    settingsPath,
    operation: "normalize",
    cause: new ForkSettingsWriteError({ problem }),
  });
});

/** `update`, then {@link check} on what it returns. */
export const withCheck =
  <E>(
    update: (current: ServerSettings) => Effect.Effect<ServerSettings, E>,
    settingsPath: string,
  ) =>
  (current: ServerSettings): Effect.Effect<ServerSettings, E | ServerSettingsError> =>
    update(current).pipe(Effect.tap((next) => check(current, next, settingsPath)));
