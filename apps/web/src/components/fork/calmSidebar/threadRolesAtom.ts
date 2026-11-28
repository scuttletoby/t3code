/**
 * Fork (calm sidebar): the web's one atom of resolved thread roles, by environment
 * (client-runtime's `createThreadRolesAtom` over the web's own catalog, settings and thread atoms).
 */
import { createThreadRolesAtom } from "@t3tools/client-runtime/state/thread-roles";

import { environmentCatalog } from "../../../connection/catalog";
import { serverEnvironment } from "../../../state/server";
import { environmentThreadShells } from "../../../state/threads";

export const forkThreadRolesAtom = createThreadRolesAtom({
  catalogValueAtom: environmentCatalog.catalogValueAtom,
  settingsValueAtom: serverEnvironment.settingsValueAtom,
  environmentThreadIndexAtom: environmentThreadShells.environmentThreadIndexAtom,
});
