// @effect-diagnostics nodeBuiltinImport:off - read at one seam step in IssueService's methods, whose callers' context is not ours to widen; a FileSystem requirement would change upstream's layer.
/**
 * Fork (`.t3code/issues.json`, fork issue 887 row 2): the repositories a project names replace the
 * ones T3 guesses from its git remotes, so a fork checkout's upstream is neither listed nor
 * writable (owner decision in fork issue 830). IssueService.ts runs this on each project's bindings
 * at one seam line. A project without the file keeps upstream's guess.
 */
import * as NodeFSP from "node:fs/promises";
import * as NodePath from "node:path";

import { FORK_ISSUES_CONFIG_PATH, ForkIssuesConfig } from "@t3tools/contracts";
import { fromLenientJson } from "@t3tools/shared/schemaJson";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

const decodeConfig = Schema.decodeUnknownEffect(fromLenientJson(ForkIssuesConfig));

/** What the file says: absent (keep the guess), the named repositories, or unreadable. */
export type IssuesConfigRead =
  | { readonly kind: "absent" }
  | { readonly kind: "named"; readonly repositories: ReadonlyArray<string> | undefined }
  | { readonly kind: "unreadable"; readonly detail: string };

export const readIssuesConfig = (workspaceRoot: string): Effect.Effect<IssuesConfigRead> => {
  const file = NodePath.join(workspaceRoot, ...FORK_ISSUES_CONFIG_PATH);
  return Effect.tryPromise({
    try: () => NodeFSP.readFile(file, "utf8"),
    catch: (cause) => String((cause as { readonly code?: unknown } | undefined)?.code ?? cause),
  }).pipe(
    Effect.flatMap((raw) =>
      decodeConfig(raw).pipe(
        Effect.map((config): IssuesConfigRead => ({
          kind: "named",
          repositories: config.repositories,
        })),
        Effect.catch((error) =>
          Effect.succeed<IssuesConfigRead>({ kind: "unreadable", detail: String(error) }),
        ),
      ),
    ),
    Effect.catch((code) =>
      Effect.succeed<IssuesConfigRead>(
        code === "ENOENT" || code === "ENOTDIR"
          ? { kind: "absent" }
          : { kind: "unreadable", detail: `The file could not be read (${code}).` },
      ),
    ),
  );
};

interface Binding {
  readonly tracker: string;
  readonly host: string;
  readonly scope: string;
}

/**
 * The bindings a project lists after its file. Standalone trackers' are kept. The git host's become
 * the named ones, in order; one not guessed is served on the guessed primary's tracker and host. An
 * unreadable file lists no git-host repository: the guess would list the upstream it leaves out.
 */
export function applyIssuesConfig<B extends Binding>(
  bindings: ReadonlyArray<B>,
  standaloneKinds: ReadonlySet<string>,
  config: IssuesConfigRead,
): ReadonlyArray<B> {
  if (config.kind === "absent") return bindings;
  if (config.kind === "named" && config.repositories === undefined) return bindings;
  const standalone = bindings.filter((binding) => standaloneKinds.has(binding.tracker));
  if (config.kind === "unreadable") return standalone;
  const gitHost = bindings.filter((binding) => !standaloneKinds.has(binding.tracker));
  const primary = gitHost[0];
  const named: B[] = [];
  const seen = new Set<string>();
  for (const scope of config.repositories ?? []) {
    const key = scope.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    const guessed = gitHost.find((binding) => binding.scope.toLowerCase() === key);
    if (guessed !== undefined) named.push(guessed);
    else if (primary !== undefined) named.push({ ...primary, scope });
  }
  return [...standalone, ...named];
}

/**
 * The seam's step: reads the project's file and applies it. A file that cannot be read is logged,
 * each time a listing meets it, so a typo shows in the server log rather than only as a missing
 * repository.
 */
export const namedRepositories =
  (
    project: { readonly workspaceRoot: string },
    registry: { readonly standalone: ReadonlyArray<{ readonly kind: string }> },
  ) =>
  <B extends Binding>(bindings: ReadonlyArray<B>): Effect.Effect<ReadonlyArray<B>> =>
    readIssuesConfig(project.workspaceRoot).pipe(
      Effect.tap((config) =>
        config.kind === "unreadable"
          ? Effect.logWarning(
              `${NodePath.join(...FORK_ISSUES_CONFIG_PATH)} could not be read; the project lists no git-host repository until it is fixed.`,
            ).pipe(
              Effect.annotateLogs({ workspaceRoot: project.workspaceRoot, detail: config.detail }),
            )
          : Effect.void,
      ),
      Effect.map((config) =>
        applyIssuesConfig(
          bindings,
          new Set(registry.standalone.map((tracker) => tracker.kind)),
          config,
        ),
      ),
    );
