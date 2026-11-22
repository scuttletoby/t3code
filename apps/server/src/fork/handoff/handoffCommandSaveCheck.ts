/**
 * Fork: a new handoff command must name a program no agent can edit: a full path to a file outside
 * any git checkout, and so must a script it hands an interpreter. One already stored at the same
 * place is never refused, so it blocks no other save (it then fails at the handoff).
 */
import { isPlaytestProgramFullyQualified, type ServerSettings } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";

/** An argument with one of these endings is a script an interpreter runs. */
const SCRIPT_ENDING = /\.(?:sh|bash|zsh|fish|py|js|mjs|cjs|ts|mts|cts|rb|pl|php|ps1|lua|tcl)$/iu;

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

/** The nearest folder above `file` (as written, and with links resolved) that holds `.git`. */
const checkoutAbove = Effect.fn("HandoffCommandSaveCheck.checkoutAbove")(function* (file: string) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const real = yield* fs.realPath(file).pipe(Effect.orElseSucceed(() => file));
  for (const start of new Set([file, real])) {
    let folder = path.dirname(start);
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

const isFile = (fs: FileSystem.FileSystem, file: string) =>
  fs.stat(file).pipe(
    Effect.map((info) => info.type === "File"),
    Effect.orElseSucceed(() => false),
  );

/** Why one command cannot be saved, or `undefined`. */
const commandProblem = Effect.fn("HandoffCommandSaveCheck.commandProblem")(function* (
  { where, argv }: PlacedCommand,
  platform: string,
) {
  const fs = yield* FileSystem.FileSystem;
  const fullPath = platform === "win32" ? "C:\\… or \\\\server\\…" : "/…";
  const [program = "", ...args] = argv;
  if (!isPlaytestProgramFullyQualified(program, platform)) {
    return `${where} runs "${program}", which is not a full path on this server's OS: give the program's full path (${fullPath}).`;
  }
  if (!(yield* isFile(fs, program))) {
    return `${where} runs "${program}", which is not a file on this server: give the full path of a program that exists.`;
  }
  // A relative script runs from the project folder, which agents edit.
  const relativeScript = args.find(
    (arg) => SCRIPT_ENDING.test(arg) && !isPlaytestProgramFullyQualified(arg, platform),
  );
  if (relativeScript !== undefined) {
    return `${where} gives "${relativeScript}", a script by a relative path: it would run from the project folder, which agents can edit. Give the full path of a script outside any repository (${fullPath}).`;
  }
  for (const file of [program, ...args]) {
    if (file !== program) {
      if (!isPlaytestProgramFullyQualified(file, platform)) continue;
      if (!(yield* isFile(fs, file))) {
        if (!SCRIPT_ENDING.test(file)) continue;
        return `${where} gives "${file}", which is not a file on this server: give the full path of a script that exists.`;
      }
    }
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
