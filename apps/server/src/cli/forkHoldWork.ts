/**
 * Fork (go-live kit): the refusals `t3 serve --hold-work` runs before config resolution, which
 * claims a port and creates directories. cli/server.ts calls `refuseHoldWork` once.
 */
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Option from "effect/Option";
import * as CliError from "effect/cli/CliError";

import { resolveServiceLauncherMode } from "../cloud/serviceLauncherClient.ts";
import { resolveBaseDir } from "../os-jank.ts";

/** The background service must never run held: its threads would silently stop working. A
 * launcher context that cannot be read refuses too. */
export const refuseHoldWorkForBootService = resolveServiceLauncherMode().pipe(
  Effect.map(({ managed }) => managed),
  Effect.orElseSucceed(() => true),
  Effect.flatMap((managed) =>
    managed
      ? Effect.fail(
          new CliError.UserError({
            cause: "hold-work",
            userMessage:
              "--hold-work is refused for the background service. Run `t3 serve --hold-work --base-dir <copy>` by hand on a copy of a T3 home.",
          }),
        )
      : Effect.void,
  ),
);

const defaultHomeRefusal = (detail: string) =>
  new CliError.UserError({
    cause: "hold-work",
    userMessage: [
      `--hold-work is refused ${detail}.`,
      "A held boot writes hold-work-copy.txt in its home, and every later boot of that home then holds T3 Connect (no tunnel, no agent activity) until someone deletes the file: on the live home that ends remote access.",
      "Run `t3 serve --hold-work --base-dir <copy>` on a copy of a T3 home. Nothing was started or written.",
    ].join("\n"),
  });

/**
 * `--hold-work` marks the home it runs on as a copy (cloud/holdWorkConnect.ts), so it needs a home
 * named with `--base-dir`, and never the default home (`~/.t3`), which is the live one on most
 * machines. `T3CODE_HOME` alone does not count: it is set in a shell, not written on the command.
 */
export const refuseHoldWorkOnDefaultHome = Effect.fn("cli.refuseHoldWorkOnDefaultHome")(function* (
  baseDirFlag: Option.Option<string>,
) {
  const named = baseDirFlag.pipe(Option.filter((value) => value.trim().length > 0));
  if (Option.isNone(named)) {
    return yield* defaultHomeRefusal("without --base-dir: it would run on this machine's T3 home");
  }
  const home = yield* resolveBaseDir(named.value);
  const defaultHome = yield* resolveBaseDir(undefined);
  // A symlink to the default home is the default home: compare real paths when both exist.
  const fs = yield* FileSystem.FileSystem;
  const real = (path: string) => fs.realPath(path).pipe(Effect.orElseSucceed(() => path));
  if (home === defaultHome || (yield* real(home)) === (yield* real(defaultHome))) {
    return yield* defaultHomeRefusal(
      `on the default T3 home (${defaultHome}): it is the live one on most machines`,
    );
  }
});

/** Every refusal of `serve --hold-work`, before anything is started or written. */
export const refuseHoldWork = (baseDirFlag: Option.Option<string>) =>
  refuseHoldWorkForBootService.pipe(Effect.andThen(refuseHoldWorkOnDefaultHome(baseDirFlag)));
