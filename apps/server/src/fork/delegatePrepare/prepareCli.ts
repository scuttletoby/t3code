// @effect-diagnostics nodeBuiltinImport:off - a plain node script, outside the Effect runtime.
/**
 * Fork: `node apps/server/src/fork/delegatePrepare/prepareCli.ts [--dir <checkout>] [--clone <url>]
 * [--branch <name>] [--base <ref>] [--json] [--paths <file>...]`. Summary on stdout, progress on
 * stderr. Exit 0 prepared or no config, 1 failed, 2 bad arguments. docs/fork/delegate-prepare.md.
 */
import * as NodePath from "node:path";
import * as NodeURL from "node:url";

import { type PrepareOptions, runPrepare } from "./delegatePrepare.ts";

export interface PrepareArgs {
  readonly options: PrepareOptions;
  readonly json: boolean;
}

/** Throws on a bad argument (the CLI's exit 2). `--paths` takes every argument up to the next flag. */
export function parsePrepareArgs(argv: ReadonlyArray<string>, cwd: string): PrepareArgs {
  let dir = cwd;
  let clone: string | undefined;
  let branch: string | undefined;
  let base: string | undefined;
  let json = false;
  const paths: Array<string> = [];
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index]!;
    const value = () => {
      const next = argv[++index];
      if (next === undefined || next.startsWith("--")) throw new Error(`${arg} needs a value`);
      return next;
    };
    if (arg === "--dir") dir = NodePath.resolve(cwd, value());
    else if (arg === "--clone") clone = value();
    else if (arg === "--branch") branch = value();
    else if (arg === "--base") base = value();
    else if (arg === "--json") json = true;
    else if (arg === "--paths") {
      while (argv[index + 1] !== undefined && !argv[index + 1]!.startsWith("--")) {
        paths.push(argv[++index]!);
      }
    } else throw new Error(`unknown argument ${arg}`);
  }
  return {
    json,
    options: {
      dir,
      paths,
      ...(clone === undefined ? {} : { clone }),
      ...(branch === undefined ? {} : { branch }),
      ...(base === undefined ? {} : { base }),
    },
  };
}

const isMain =
  process.argv[1] !== undefined &&
  import.meta.url === NodeURL.pathToFileURL(NodePath.resolve(process.argv[1])).href;
if (isMain) {
  let args: PrepareArgs | undefined;
  try {
    args = parsePrepareArgs(process.argv.slice(2), process.cwd());
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 2;
  }
  if (args !== undefined) {
    const { json } = args;
    const outcome = await runPrepare({
      ...args.options,
      stopOnSignals: true,
      log: (text) => process.stderr.write(text),
    });
    process.stdout.write(json ? `${JSON.stringify(outcome.result, null, 2)}\n` : outcome.text);
    process.exitCode = outcome.exitCode;
  }
}
