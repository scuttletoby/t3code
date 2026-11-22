/**
 * Fork (placement): the settings load does not check `factsCommand` against this OS, so placement
 * code must read `placement` through {@link readPlacementForThisOs}, never `settings.placement`.
 * A runner of `factsCommand` on another host still checks the path there before it runs it.
 */
import {
  type PlacementSettings,
  type ServerSettings,
  placementOnPlatform,
} from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Effect from "effect/Effect";

/**
 * `settings.placement` on this server's OS (`HostProcess.Platform`): a `factsCommand` this OS cannot
 * run reads as off, kept as written with its problem (contracts' `placementOnPlatform`); a value the
 * file already held as unreadable, or a good one, is returned as it is, so it reads again the same.
 */
export const readPlacementForThisOs = (
  settings: Pick<ServerSettings, "placement">,
): Effect.Effect<PlacementSettings> =>
  Effect.map(HostProcess.Platform, (platform) => placementOnPlatform(settings.placement, platform));
