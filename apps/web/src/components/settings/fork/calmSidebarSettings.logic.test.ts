// Fork (calm sidebar): the Settings list of thread roles, which pointers it marks and what Remove
// saves. Each test names the question it answers.
import type { ThreadRoles } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  removeRolePatch,
  roleEntryText,
  roleListEntries,
  type RoleListThread,
} from "./calmSidebarSettings.logic";

const topLevel = { parentThreadId: null, relationshipToParent: null, rootThreadId: "t" } as never;
const thread = (id: string, projectId: string, changes: Partial<RoleListThread> = {}) =>
  ({
    id,
    projectId,
    title: `Thread ${id}`,
    lineage: topLevel,
    delegatedFrom: null,
    archivedAt: null,
    deletedAt: null,
    ...changes,
  }) as RoleListThread;

const ROLES = {
  assistant: "helper",
  orchestrators: {
    "project-game": "lead",
    "project-site": "gone",
    "project-docs": "archived",
    "project-app": "wanderer",
    "project-api": "subagent",
    "project-web": "deleted",
  },
} as unknown as ThreadRoles;

const THREADS = [
  thread("helper", "project-scratch"),
  thread("lead", "project-game"),
  thread("archived", "project-docs", { archivedAt: "2026-11-01T00:00:00.000Z" }),
  thread("wanderer", "project-game"),
  thread("subagent", "project-api", {
    lineage: { parentThreadId: "p", relationshipToParent: "subagent", rootThreadId: "p" } as never,
  }),
  thread("deleted", "project-web", { deletedAt: "2026-11-02T00:00:00.000Z" }),
];

describe("thread roles list", () => {
  it("no roles, or settings not loaded: an empty list", () => {
    expect(roleListEntries(null, THREADS)).toEqual([]);
    expect(roleListEntries({ assistant: null, orchestrators: {} } as ThreadRoles, [])).toEqual([]);
  });

  it("lists the assistant first, then each orchestrator, each marked when it gives no role now", () => {
    expect(
      roleListEntries(ROLES, THREADS).map((entry) => [entry.key, entry.title, entry.problem]),
    ).toEqual([
      ["assistant", "Thread helper", null],
      ["orchestrator:project-game", "Thread lead", null],
      ["orchestrator:project-site", null, "missing"],
      ["orchestrator:project-docs", "Thread archived", "archived"],
      ["orchestrator:project-app", "Thread wanderer", "moved"],
      ["orchestrator:project-api", "Thread subagent", "child"],
      ["orchestrator:project-web", "Thread deleted", "deleted"],
    ]);
  });

  it("names the project, and stands in for a thread or project this client does not have", () => {
    const [assistant, game, site] = roleListEntries(ROLES, THREADS);
    const title = (projectId: string) => (projectId === "project-game" ? "Game" : null);
    expect(roleEntryText(assistant!, title)).toEqual({
      role: "Assistant",
      thread: "Thread helper",
      problem: null,
    });
    expect(roleEntryText(game!, title).role).toBe("Orchestrator of Game");
    expect(roleEntryText(site!, title)).toEqual({
      role: "Orchestrator of project project-site",
      thread: "thread gone",
      problem: "missing: no such thread here",
    });
  });

  it("Remove clears that one pointer alone", () => {
    const [assistant, game] = roleListEntries(ROLES, THREADS);
    expect(removeRolePatch(assistant!)).toEqual({ threadRoles: { assistant: null } });
    expect(removeRolePatch(game!)).toEqual({
      threadRoles: { orchestrators: { "project-game": null } },
    });
  });
});
