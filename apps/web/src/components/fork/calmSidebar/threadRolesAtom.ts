/**
 * Fork (calm sidebar): the one resolved-roles atom of this app, over upstream's own atoms (the
 * environment catalog, each server's settings, each server's thread index).
 */
import { createThreadRolesAtom } from "@t3tools/client-runtime/state/thread-roles";

import { environmentCatalog } from "../../../connection/catalog";
import { serverEnvironment } from "../../../state/server";
import { environmentThreadShells } from "../../../state/threads";

export const threadRolesAtom = createThreadRolesAtom({
  catalogValueAtom: environmentCatalog.catalogValueAtom,
  settingsValueAtom: serverEnvironment.settingsValueAtom,
  environmentThreadIndexAtom: environmentThreadShells.environmentThreadIndexAtom,
});
