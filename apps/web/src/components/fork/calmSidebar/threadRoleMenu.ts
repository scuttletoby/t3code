/**
 * Fork (calm sidebar): the thread menu's Role entry, as both thread menus (the sidebar's and the
 * thread title's) call it: one call fills the entry's state, one runs a pick. It imports nothing of
 * the app graph, so upstream's hook and its tests load it as they are; the reads and saves are in
 * threadRoleMenuRuntime.ts, installed when the app loads it. Before that: no Role entry.
 */
import type { ScopedThreadRef } from "@t3tools/contracts";

import {
  isThreadRoleMenuId,
  type ThreadRoleMenuId,
  type ThreadRoleMenuState,
} from "./threadRoleMenu.logic";

interface ThreadRoleMenuRuntime {
  readonly read: (threadRef: ScopedThreadRef) => ThreadRoleMenuState | undefined;
  readonly run: (threadRef: ScopedThreadRef, id: ThreadRoleMenuId) => void;
}

let runtime: ThreadRoleMenuRuntime | null = null;

export function installThreadRoleMenuRuntime(next: ThreadRoleMenuRuntime): void {
  runtime = next;
}

/** The Role entry's state for the thread menu, or none. Synchronous, so the menu opens at once. */
export function readThreadRoleMenuState(
  threadRef: ScopedThreadRef,
): ThreadRoleMenuState | undefined {
  return runtime?.read(threadRef);
}

/** Runs a Role pick and answers true; any other menu id answers false (the caller's own). */
export function runIfThreadRoleMenuAction(threadRef: ScopedThreadRef, id: string): boolean {
  if (!isThreadRoleMenuId(id)) return false;
  runtime?.run(threadRef, id);
  return true;
}
