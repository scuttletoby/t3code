/**
 * Fork: the parsed settings file before upstream's decode: old auto-update names renamed, and an
 * unusable interval left out with a warning, so it reads as its default instead of failing the
 * whole file (which resets every setting). The next save removes it from the file.
 */
import { dropUnusableStoredIntervals } from "@t3tools/shared/forkSettingsPatch";
import { fromLenientJson } from "@t3tools/shared/schemaJson";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Logger from "effect/Logger";
import * as Schema from "effect/Schema";

import { renameRetiredAutoUpdateKeys } from "../cloud/autoUpdateSettings.ts";

/** The parsed file as the decode reads it, and each interval left out; pure, for the fork report. */
export const readSettingsFile = (parsed: unknown) =>
  dropUnusableStoredIntervals(renameRetiredAutoUpdateKeys(parsed));

const warnUnusableIntervals = (dropped: ReadonlyArray<unknown>) =>
  dropped.length === 0
    ? Effect.void
    : Effect.logWarning(
        "settings: unusable intervals in settings.json read as their defaults; the next settings save removes them from the file",
        { dropped },
      );

/** For upstream's synchronous `decodeSettingsFileText`: warns to Effect's default logger. */
export function prepareSettingsFile(parsed: unknown): unknown {
  const { settings, dropped } = readSettingsFile(parsed);
  Effect.runSync(warnUnusableIntervals(dropped));
  return settings;
}

// The load's lenient parse (comments, trailing commas), as serverSettings.ts parses.
const parseSettingsFileJsonExit = Schema.decodeUnknownExit(fromLenientJson(Schema.Unknown));

/**
 * Upstream's `decodeSettingsFileText`, step for step, in the caller's fiber: what an interval or a
 * lenient field drops is logged to the caller's logger (the server's on a load), not to Effect's
 * console logger. `decode` is serverSettings.ts's `decodePersistedSettings` (its schema is private).
 */
export function decodeSettingsFile<A, E>(
  raw: string,
  withDefaults: (persisted: unknown) => unknown,
  decode: (persisted: unknown) => Effect.Effect<A, E>,
): Effect.Effect<Exit.Exit<A, E | Schema.SchemaError>> {
  const parsed = parseSettingsFileJsonExit(raw);
  if (Exit.isFailure(parsed)) return Effect.succeed(Exit.failCause(parsed.cause));
  const { settings, dropped } = readSettingsFile(parsed.value);
  const persisted = withDefaults(settings);
  return Effect.exit(Effect.andThen(warnUnusableIntervals(dropped), decode(persisted)));
}

/**
 * The load's log of {@link decodeSettingsFile}: upstream's `decodeSettingsFile` in serverSettings.ts
 * reads the file silently (decodeSettingsFileSilently), so the load runs this first, in its fiber.
 */
export const logSettingsFileRead = <A, E>(
  raw: string,
  withDefaults: (persisted: unknown) => unknown,
  decode: (persisted: unknown) => Effect.Effect<A, E>,
): Effect.Effect<void> => Effect.asVoid(decodeSettingsFile(raw, withDefaults, decode));

const layerNoLoggers = Logger.layer([], { mergeWithExisting: false });

/**
 * {@link decodeSettingsFile}, synchronous and silent, for second readers of the file (`__fork-report`,
 * whose stdout is its JSON, and the Claude MCP profile reader): they decode exactly as a load does
 * without repeating its warnings.
 */
export const decodeSettingsFileSilently = <A, E>(
  raw: string,
  withDefaults: (persisted: unknown) => unknown,
  decode: (persisted: unknown) => Effect.Effect<A, E>,
): Exit.Exit<A, E | Schema.SchemaError> =>
  Effect.runSync(
    decodeSettingsFile(raw, withDefaults, decode).pipe(Effect.provide(layerNoLoggers)),
  );
