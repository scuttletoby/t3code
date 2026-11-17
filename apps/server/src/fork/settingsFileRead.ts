/**
 * Fork: the parsed settings file before upstream's decode: old auto-update names renamed, and an
 * unusable interval left out with a warning, so it reads as its default instead of failing the
 * whole file (which resets every setting). The next save removes it from the file.
 */
import { dropUnusableStoredIntervals } from "@t3tools/shared/forkSettingsPatch";
import * as Effect from "effect/Effect";

import { renameRetiredAutoUpdateKeys } from "../cloud/autoUpdateSettings.ts";

export function prepareSettingsFile(parsed: unknown): unknown {
  const { settings, dropped } = dropUnusableStoredIntervals(renameRetiredAutoUpdateKeys(parsed));
  if (dropped.length > 0) {
    // The decode is synchronous; its own warnings (wait checks) are logged the same way.
    Effect.runSync(
      Effect.logWarning(
        "settings: unusable intervals in settings.json read as their defaults; the next settings save removes them from the file",
        { dropped },
      ),
    );
  }
  return settings;
}
