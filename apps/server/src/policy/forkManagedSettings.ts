/**
 * Fork: the fork settings that read any bad value, null included, as off or a default. Upstream
 * refuses a policy path inside such a setting, so a policy manages each only as a whole. A test
 * (managedSettings.forkKeys.test.ts) pins this list to exactly the fork settings that read null.
 */
import type { ForkServerSettingKey } from "@t3tools/contracts";

const WHOLE_ONLY = [
  "accountOrder",
  "assistant",
  "handoff",
  "mcpCarriedProfiles",
  "mcpProfile",
  "mcpProfiles",
  "mcpSubagentProfile",
  "mcpThreadProfiles",
  "modelPolicy",
  "placement",
  "placementNeeds",
  "playtest",
  "reviewRules",
  "stopFinishedChildren",
  "threadRoles",
] as const satisfies ReadonlyArray<ForkServerSettingKey>;

export const FORK_WHOLE_MANAGED_SETTINGS: ReadonlySet<string> = new Set(WHOLE_ONLY);
