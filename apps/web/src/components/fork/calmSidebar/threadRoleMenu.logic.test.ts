// Fork (calm sidebar): the thread menu's Role entry, which picks it offers for a thread and the
// one settings patch each pick saves. Each test names the question it answers.
import type { ThreadRoles } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  isThreadRoleMenuId,
  MAKE_ASSISTANT,
  MAKE_ORCHESTRATOR,
  REMOVE_ROLE,
  THREAD_ROLE_MENU,
  threadRoleMenuItems,
  threadRoleMenuState,
  threadRolePatch,
} from "./threadRoleMenu.logic";

const topLevel = { parentThreadId: null, relationshipToParent: null, rootThreadId: "t" } as never;
const thread = (id: string, projectId = "project-game", changes: object = {}) => ({
  id,
  projectId,
  lineage: topLevel,
  delegatedFrom: null,
  deletedAt: null,
  ...changes,
});
const roles = (assistant: string | null, orchestrators: Record<string, string> = {}) =>
  ({ assistant, orchestrators }) as ThreadRoles;
const TITLES: Record<string, string> = { lead: "Game lead", helper: "Helper" };
const state = (
  self: ReturnType<typeof thread> | null,
  value: ThreadRoles | null,
  canWriteSettings = true,
) =>
  threadRoleMenuState({
    thread: self,
    roles: value,
    canWriteSettings,
    titleOf: (id) => TITLES[id] ?? null,
  });
const picks = (value: ReturnType<typeof state>) =>
  threadRoleMenuItems(value)[0]?.children?.map((item) => [
    item.id,
    item.label,
    item.checked === true ? "checked, disabled" : item.detail,
  ]);

describe("thread menu Role entry", () => {
  it("no entry where no role can be set: no settings right, settings not loaded, a child or a deleted thread", () => {
    expect(state(thread("a"), roles(null), false)).toBeUndefined();
    expect(state(thread("a"), null)).toBeUndefined();
    expect(state(null, roles(null))).toBeUndefined();
    const subagent = { parentThreadId: "p", relationshipToParent: "subagent", rootThreadId: "p" };
    expect(state(thread("a", "project-game", { lineage: subagent }), roles(null))).toBeUndefined();
    expect(state(thread("a", "project-game", { delegatedFrom: {} }), roles(null))).toBeUndefined();
    expect(state(thread("a", "project-game", { deletedAt: "x" }), roles(null))).toBeUndefined();
    expect(threadRoleMenuItems(undefined)).toEqual([]);
  });

  it("a thread with no role and no holders: Make project orchestrator and Make assistant", () => {
    expect(picks(state(thread("a"), roles(null)))).toEqual([
      [MAKE_ORCHESTRATOR, "Make project orchestrator", undefined],
      [MAKE_ASSISTANT, "Make assistant", undefined],
    ]);
    expect(threadRoleMenuItems(state(thread("a"), roles(null)))[0]).toMatchObject({
      id: THREAD_ROLE_MENU,
      label: "Role",
    });
  });

  it("another thread holds the role: Replace, naming the holder (or a stand-in when not loaded)", () => {
    expect(picks(state(thread("a"), roles("ghost", { "project-game": "lead" })))).toEqual([
      [MAKE_ORCHESTRATOR, "Replace project orchestrator", "now Game lead"],
      [MAKE_ASSISTANT, "Replace assistant", "now a thread not loaded"],
    ]);
  });

  it("the thread holds a role: it shows checked and disabled, and Remove role is offered", () => {
    expect(picks(state(thread("lead"), roles(null, { "project-game": "lead" })))).toEqual([
      [MAKE_ORCHESTRATOR, "Project orchestrator", "checked, disabled"],
      [MAKE_ASSISTANT, "Make assistant", undefined],
      [REMOVE_ROLE, "Remove role", undefined],
    ]);
    expect(picks(state(thread("lead"), roles("lead", { "project-game": "lead" })))).toEqual([
      [MAKE_ORCHESTRATOR, "Project orchestrator", "checked, disabled"],
      [MAKE_ASSISTANT, "Assistant", "checked, disabled"],
      [REMOVE_ROLE, "Remove role", undefined],
    ]);
    const held = threadRoleMenuItems(state(thread("lead"), roles("lead")))[0]?.children?.[1];
    expect(held).toMatchObject({ checked: true, disabled: true });
  });

  it("a thread that moved keeps an old project's pointer: Remove role clears it too", () => {
    const moved = state(thread("lead", "project-site"), roles(null, { "project-game": "lead" }));
    expect(moved?.holdsOtherProjectRole).toBe(true);
    expect(picks(moved)?.map(([id]) => id)).toContain(REMOVE_ROLE);
  });

  it("Make saves one pointer; the server merges it per key", () => {
    const self = thread("a");
    expect(threadRolePatch(MAKE_ORCHESTRATOR, self, roles(null))).toEqual({
      threadRoles: { orchestrators: { "project-game": "a" } },
    });
    expect(threadRolePatch(MAKE_ASSISTANT, self, roles("lead"))).toEqual({
      threadRoles: { assistant: "a" },
    });
  });

  it("Remove clears every pointer naming the thread, and leaves the others alone", () => {
    const value = roles("a", { "project-game": "a", "project-old": "a", "project-site": "b" });
    expect(threadRolePatch(REMOVE_ROLE, thread("a"), value)).toEqual({
      threadRoles: {
        assistant: null,
        orchestrators: { "project-game": null, "project-old": null },
      },
    });
    expect(threadRolePatch(REMOVE_ROLE, thread("b"), roles("a", {}))).toBeNull();
    expect(threadRolePatch(THREAD_ROLE_MENU, thread("a"), value)).toBeNull();
  });

  it("knows its own ids, and no other menu's", () => {
    expect([THREAD_ROLE_MENU, MAKE_ASSISTANT, REMOVE_ROLE].every(isThreadRoleMenuId)).toBe(true);
    expect(isThreadRoleMenuId("copy")).toBe(false);
    expect(isThreadRoleMenuId("fork-roles")).toBe(false);
  });
});
