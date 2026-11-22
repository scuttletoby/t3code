/**
 * Fork: a new handoff command must name a program, and a script, no agent can edit: full paths
 * outside any git checkout. One already stored at the same place is never refused. A guard against
 * mistakes, not attacks: docs/fork/handoff.md lists what it does not catch.
 */
import { isPlaytestProgramFullyQualified, type ServerSettings } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";

/** An argument with one of these endings is a script an interpreter runs. */
const SCRIPT_ENDING = /\.(?:sh|bash|zsh|fish|py|js|mjs|cjs|ts|mts|cts|rb|pl|php|ps1|lua|tcl)$/iu;

/**
 * Programs whose first argument that is not an option is the script they run, whatever its
 * ending: shells and language runtimes, by file name (a version and `.exe` left off).
 */
const INTERPRETER =
  /^(?:(?:ba|z|da|k|c|tc|fi)?sh|fish|python|pypy|node|nodejs|ruby|perl|php|lua|luajit|tclsh|pwsh|powershell|rscript)$/u;
/** Options after which an interpreter runs code given inline, not a file (`sh -c`, `node -e`). */
const INLINE_CODE = new Set(["-c", "-e", "--eval", "-p", "--print", "-command"]);
/** Interpreter options that take the next argument as their value, not as the script. */
const TAKES_VALUE: Record<string, ReadonlySet<string>> = {
  python: new Set(["-W", "-X"]),
  pypy: new Set(["-W", "-X"]),
  node: new Set(["-C", "--conditions", "--inspect-port", "--title"]),
};
/** Node options that load a module: a name or relative path loads it from the project folder. */
const LOADS_MODULE = new Set(["-r", "--require", "--import", "--loader", "--experimental-loader"]);
/**
 * Programs that run what a file in their working folder says (a Makefile, package.json,
 * Cargo.toml and the like): the command runs in the project folder, which agents edit.
 */
const PROJECT_RUNNER =
  /^(?:g?make|just|rake|npm|npx|pnpm|pnpx|yarn|cargo|go|gradlew?|mvnw?|ant|task|tox|nox|poetry|pipenv|uv|uvx|composer)$/u;

/**
 * Runtimes that read their config from their working folder before any script: `bunfig.toml`
 * (its `preload` runs first), `deno.json` (its import map can swap what a script imports). The
 * command runs in the project folder, which agents edit, so a safe script does not make them safe.
 */
const PROJECT_CONFIG_RUNTIME = /^(?:bun|bunx|deno)$/u;

/**
 * Whether R reads `.Rprofile` (sourced first) and `.Renviron` from its working folder, the project
 * folder. `R` itself is refused whatever it is given (its `R CMD` tools read project files of their
 * own); `Rscript` unless `--vanilla` comes among its options, before its script or `-e` (after
 * them, it is an argument of the script).
 */
const readsRProfile = (name: string, args: ReadonlyArray<string>) => {
  if (name === "r") return true;
  if (name !== "rscript") return false;
  for (const arg of args) {
    if (arg === "--vanilla") return false;
    if (!arg.startsWith("-") || arg === "-e") return true;
  }
  return true;
};

/** A program's name as the checks compare it: its last path part, lower case, no `.exe` or version. */
const programName = (program: string) =>
  (program.split(/[\\/]/u).at(-1) ?? "")
    .toLowerCase()
    .replace(/\.(?:exe|cmd|bat)$/u, "")
    .replace(/[\d.]+$/u, "");

interface PlacedCommand {
  /** Where the settings hold it, as the refusal names it. */
  readonly where: string;
  readonly argv: ReadonlyArray<string>;
}

/** Every handoff command in `settings`: the server's and each project's. */
const commandsIn = (settings: ServerSettings): ReadonlyArray<PlacedCommand> => [
  ...(settings.handoff.command === undefined
    ? []
    : [{ where: "handoff.command", argv: settings.handoff.command }]),
  ...Object.entries(settings.projectSettingsOverrides).flatMap(([projectId, override]) => {
    const argv = override?.handoff?.command;
    return argv === undefined
      ? []
      : [{ where: `projectSettingsOverrides.${projectId}.handoff.command`, argv }];
  }),
];

/**
 * The nearest folder that holds `.git`, from `file` up (as written, and with links resolved):
 * a folder given is checked itself too.
 */
const checkoutAbove = Effect.fn("HandoffCommandSaveCheck.checkoutAbove")(function* (file: string) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const real = yield* fs.realPath(file).pipe(Effect.orElseSucceed(() => file));
  for (const start of new Set([file, real])) {
    let folder = (yield* isFolder(fs, start)) ? start : path.dirname(start);
    while (true) {
      // `.git` is a folder in a clone and a file in a worktree: either marks a checkout.
      const marked = yield* fs
        .exists(path.join(folder, ".git"))
        .pipe(Effect.orElseSucceed(() => false));
      if (marked) return folder;
      const parent = path.dirname(folder);
      if (parent === folder) break;
      folder = parent;
    }
  }
  return undefined;
});

const isType = (fs: FileSystem.FileSystem, file: string, type: "File" | "Directory") =>
  fs.stat(file).pipe(
    Effect.map((info) => info.type === type),
    Effect.orElseSucceed(() => false),
  );
const isFile = (fs: FileSystem.FileSystem, file: string) => isType(fs, file, "File");
const isFolder = (fs: FileSystem.FileSystem, file: string) => isType(fs, file, "Directory");

/**
 * What an interpreter's arguments make it run: a script (the first argument that is not an
 * option, or a folder Python runs by its `__main__.py`), code given inline (nothing to check), or
 * a module found by name (`python -m`, `node -r`), which is looked up from the project folder.
 */
function interpreterTarget(
  name: string,
  args: ReadonlyArray<string>,
  platform: string,
):
  | { readonly kind: "script"; readonly file: string }
  | { readonly kind: "inline" }
  | { readonly kind: "module"; readonly option: string; readonly module: string }
  | { readonly kind: "none" } {
  for (let index = 0; index < args.length; index++) {
    const arg = args[index]!;
    if (arg === "--") {
      const file = args[index + 1];
      return file === undefined ? { kind: "none" } : { kind: "script", file };
    }
    if (INLINE_CODE.has(arg.toLowerCase())) return { kind: "inline" };
    if (name.startsWith("py") && arg.startsWith("-m")) {
      return {
        kind: "module",
        option: "-m",
        module: arg === "-m" ? (args[index + 1] ?? "") : arg.slice(2),
      };
    }
    const [option = arg, inline] = arg.split("=", 2);
    if (LOADS_MODULE.has(option)) {
      const module = inline ?? args[index + 1] ?? "";
      if (!isPlaytestProgramFullyQualified(module, platform)) {
        return { kind: "module", option, module };
      }
      if (inline === undefined) index++;
      continue;
    }
    if (arg.startsWith("-")) {
      if (inline === undefined && TAKES_VALUE[name]?.has(arg) === true) index++;
      continue;
    }
    return { kind: "script", file: arg };
  }
  return { kind: "none" };
}

/** env's long options that take a value; `getopt` also takes any unique start of their names. */
const ENV_LONG_WITH_VALUE = ["unset", "argv0"];
/** env's options this check cannot follow: -S splits a string into a command, -C changes folder. */
const ENV_LONG_UNFOLLOWABLE = ["split-string", "chdir"];
/** env's short options that take a value (GNU -u -S -C, newer GNU -a; BSD -P -L), attached or next. */
const ENV_SHORT_WITH_VALUE = new Set(["u", "a", "S", "C", "P", "L"]);
const ENV_SHORT_UNFOLLOWABLE = new Set(["S", "C"]);

/**
 * What one word before env's program is: an option of env's (one whose value is the next word,
 * or one this check cannot follow) or `undefined` (a NAME=value pair or the program). Short options
 * may be grouped and take their value attached (`-iS'cmd'`), long ones may be shortened
 * (`--split=cmd`), as GNU env reads them.
 */
const envOption = (arg: string): "flag" | "valueNext" | "unfollowable" | undefined => {
  if (arg === "--") return "flag";
  if (arg.startsWith("--")) {
    const [name = "", value] = arg.slice(2).split(/=(.*)/su);
    const isStartOf = (full: string) => full.startsWith(name);
    if (ENV_LONG_UNFOLLOWABLE.some(isStartOf)) return "unfollowable";
    return ENV_LONG_WITH_VALUE.some(isStartOf) && value === undefined ? "valueNext" : "flag";
  }
  if (!arg.startsWith("-") || arg === "-") return arg === "-" ? "flag" : undefined;
  for (let at = 1; at < arg.length; at += 1) {
    const letter = arg[at]!;
    if (ENV_SHORT_UNFOLLOWABLE.has(letter)) return "unfollowable";
    // The rest of the word is this option's value, or, when nothing is left, the next word is.
    if (ENV_SHORT_WITH_VALUE.has(letter)) return at === arg.length - 1 ? "valueNext" : "flag";
  }
  return "flag";
};

/** Why one command cannot be saved, or `undefined`. */
const commandProblem = Effect.fn("HandoffCommandSaveCheck.commandProblem")(function* (
  { where, argv }: PlacedCommand,
  platform: string,
): Effect.fn.Return<string | undefined, never, FileSystem.FileSystem | Path.Path> {
  const fs = yield* FileSystem.FileSystem;
  const fullPath = platform === "win32" ? "C:\\… or \\\\server\\…" : "/…";
  const [program = "", ...args] = argv;
  if (!isPlaytestProgramFullyQualified(program, platform)) {
    return `${where} runs "${program}", which is not a full path on this server's OS: give the program's full path (${fullPath}).`;
  }
  if (!(yield* isFile(fs, program))) {
    return `${where} runs "${program}", which is not a file on this server: give the full path of a program that exists.`;
  }
  const programCheckout = yield* checkoutAbove(program);
  if (programCheckout !== undefined) {
    return `${where} runs "${program}", which is inside the git checkout ${programCheckout}: an agent can edit it. Move it outside any repository and give that full path.`;
  }
  const name = programName(program);
  // `env` runs the program after its options and NAME=value pairs: that one is checked instead.
  if (name === "env") {
    let index = 0;
    while (index < args.length) {
      const arg = args[index]!;
      const option = envOption(arg);
      if (option === "unfollowable") {
        return `${where} gives env "${arg}", which this check cannot follow: run the program by its full path, without env.`;
      }
      if (option === "valueNext") index += 2;
      else if (option === "flag" || /^[^=]+=/u.test(arg)) index += 1;
      else break;
    }
    const rest = args.slice(index);
    if (rest.length === 0) return undefined;
    if (!isPlaytestProgramFullyQualified(rest[0]!, platform)) {
      return `${where} runs "${rest[0]}" through env, which finds it on the PATH: give its full path (${fullPath}).`;
    }
    return yield* commandProblem({ where, argv: rest }, platform);
  }
  if (PROJECT_RUNNER.test(name)) {
    return `${where} runs "${program}", which runs what a file in the project folder (a Makefile, package.json and the like) says, and agents can edit that folder. Give the full path of a program or script outside any repository instead.`;
  }
  if (PROJECT_CONFIG_RUNTIME.test(name)) {
    return `${where} runs "${program}", which reads its config (bunfig.toml, deno.json and the like) from the project folder, and agents can edit that folder: it can run project code before any script. Give the full path of another program or script outside any repository instead.`;
  }
  if (readsRProfile(name, args)) {
    return `${where} runs "${program}", which reads .Rprofile and .Renviron from the project folder, and agents can edit that folder: it can run project code before any script. Give Rscript --vanilla before its script, or the full path of another program or script outside any repository.`;
  }
  const scripts: Array<string> = [];
  if (INTERPRETER.test(name)) {
    const target = interpreterTarget(name, args, platform);
    if (target.kind === "module") {
      return `${where} gives "${target.option} ${target.module}", a module found by name: it is looked up from the project folder, which agents can edit. Give the full path of a script outside any repository instead.`;
    }
    if (target.kind === "script") {
      if (!isPlaytestProgramFullyQualified(target.file, platform)) {
        return `${where} gives "${target.file}", a script by a relative path: it would run from the project folder, which agents can edit. Give the full path of a script outside any repository (${fullPath}).`;
      }
      if (!(yield* isFile(fs, target.file)) && !(yield* isFolder(fs, target.file))) {
        return `${where} gives "${target.file}", which is not a file on this server: give the full path of a script that exists.`;
      }
      scripts.push(target.file);
    }
  }
  // Any program: an argument that reads as a script (by its ending) is checked the same way.
  const relativeScript = args.find(
    (arg) => SCRIPT_ENDING.test(arg) && !isPlaytestProgramFullyQualified(arg, platform),
  );
  if (relativeScript !== undefined) {
    return `${where} gives "${relativeScript}", a script by a relative path: it would run from the project folder, which agents can edit. Give the full path of a script outside any repository (${fullPath}).`;
  }
  for (const arg of args) {
    if (scripts.includes(arg) || !isPlaytestProgramFullyQualified(arg, platform)) continue;
    if (yield* isFile(fs, arg)) scripts.push(arg);
    else if (SCRIPT_ENDING.test(arg)) {
      return `${where} gives "${arg}", which is not a file on this server: give the full path of a script that exists.`;
    }
  }
  for (const file of scripts) {
    const checkout = yield* checkoutAbove(file);
    if (checkout !== undefined) {
      return `${where} runs "${file}", which is inside the git checkout ${checkout}: an agent can edit it. Move it outside any repository and give that full path.`;
    }
  }
  return undefined;
});

/**
 * The first handoff command `next` sets anew (not as `current` holds it at the same place) that
 * this server (`platform`, Node's `process.platform` name) would refuse to run as safe, or
 * `undefined`.
 */
export const handoffCommandSaveProblem = Effect.fn("HandoffCommandSaveCheck.problem")(function* (
  current: ServerSettings,
  next: ServerSettings,
  platform: string,
) {
  const stored = new Map(commandsIn(current).map(({ where, argv }) => [where, argv.join("\0")]));
  for (const command of commandsIn(next)) {
    if (stored.get(command.where) === command.argv.join("\0")) continue;
    const problem = yield* commandProblem(command, platform);
    if (problem !== undefined) return problem;
  }
  return undefined;
});
