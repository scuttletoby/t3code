// @effect-diagnostics nodeBuiltinImport:off - the account's home comes from the passwd entry.
/**
 * Fork (go-live kit): the refusals `t3 serve --hold-work` runs before config resolution, which
 * claims a port and creates directories. cli/server.ts calls `refuseHoldWork` once.
 */
import * as NodeOS from "node:os";

import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
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

/** The account's home from its passwd entry, which ignores `$HOME` (null when there is none). */
export const AccountHomeDirectory = Context.Reference<string | null>(
  "t3/cli/forkHoldWork/AccountHomeDirectory",
  {
    defaultValue: () => {
      try {
        return NodeOS.userInfo().homedir || null;
      } catch {
        return null;
      }
    },
  },
);

/**
 * The real path of `path`, also when it does not exist yet: the deepest folder that exists is
 * resolved and the rest appended, so a symlinked parent cannot hide a home not made yet.
 */
const realPathOrParent = Effect.fn("cli.forkHoldWork.realPathOrParent")(function* (path: string) {
  const fs = yield* FileSystem.FileSystem;
  const { basename, dirname, join } = yield* Path.Path;
  const rest: Array<string> = [];
  let current = path;
  for (;;) {
    const real = yield* fs.realPath(current).pipe(Effect.option);
    if (Option.isSome(real)) return join(real.value, ...rest.toReversed());
    const parent = dirname(current);
    if (parent === current) return path;
    rest.push(basename(current));
    current = parent;
  }
});

/** The same folder by identity (device and inode), for spellings and mounts a path cannot show. */
const sameFolder = Effect.fn("cli.forkHoldWork.sameFolder")(function* (a: string, b: string) {
  const fs = yield* FileSystem.FileSystem;
  const both = yield* Effect.all([fs.stat(a), fs.stat(b)]).pipe(Effect.option);
  if (Option.isNone(both)) return false;
  const [left, right] = both.value;
  return (
    left.dev === right.dev &&
    Option.isSome(left.ino) &&
    Option.isSome(right.ino) &&
    left.ino.value === right.ino.value
  );
});

/**
 * `--hold-work` marks its home as a copy (cloud/holdWorkConnect.ts): it needs `--base-dir` (not
 * just `T3CODE_HOME`) and never a default home, `$HOME/.t3` or the account's own `.t3`, since a
 * scratch `$HOME` does not move the live one.
 */
export const refuseHoldWorkOnDefaultHome = Effect.fn("cli.refuseHoldWorkOnDefaultHome")(function* (
  baseDirFlag: Option.Option<string>,
) {
  const named = baseDirFlag.pipe(Option.filter((value) => value.trim().length > 0));
  if (Option.isNone(named)) {
    return yield* defaultHomeRefusal("without --base-dir: it would run on this machine's T3 home");
  }
  const { join } = yield* Path.Path;
  const home = yield* resolveBaseDir(named.value);
  const accountHome = yield* AccountHomeDirectory;
  const defaultHomes = [
    yield* resolveBaseDir(undefined),
    ...(accountHome === null ? [] : [join(accountHome, ".t3")]),
  ];
  const realHome = yield* realPathOrParent(home);
  for (const defaultHome of defaultHomes) {
    // A symlink to the default home is the default home: compare real paths, then identities.
    if (
      home === defaultHome ||
      realHome === (yield* realPathOrParent(defaultHome)) ||
      (yield* sameFolder(home, defaultHome))
    ) {
      return yield* defaultHomeRefusal(
        `on the default T3 home (${defaultHome}): it is the live one on most machines`,
      );
    }
  }
});

/** Every refusal of `serve --hold-work`, before anything is started or written. */
export const refuseHoldWork = (baseDirFlag: Option.Option<string>) =>
  refuseHoldWorkForBootService.pipe(Effect.andThen(refuseHoldWorkOnDefaultHome(baseDirFlag)));
