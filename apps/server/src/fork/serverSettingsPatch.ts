/**
 * Fork: upstream's `applyServerSettingsPatch`, then the fork's project keys a project's entry
 * leaves out kept as stored (`keepLeftOutForkProjectKeys`). The server's settings file imports this
 * under upstream's name, so every write path (and the test layer) uses it with no other line changed.
 */
import type { ServerSettings, ServerSettingsPatch } from "@t3tools/contracts";
import { keepLeftOutForkProjectKeys } from "@t3tools/shared/forkSettingsPatch";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";

export const applyServerSettingsPatchKeepingForkProjectKeys = (
  current: ServerSettings,
  patch: ServerSettingsPatch,
): ServerSettings =>
  keepLeftOutForkProjectKeys(current, patch, applyServerSettingsPatch(current, patch));
