// The Settings list of thread roles (CAL4): does each pointer show the thread it names, and is a
// pointer that holds no role now marked with why, without being dropped?
import { ProjectId, ThreadId, type ThreadRoles } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import { threadRoleEntries } from "./calmSidebarSettings.logic";

const game = ProjectId.make("project-game");
const site = ProjectId.make("project-site");
const gone = ProjectId.make("project-gone");

const thread = (id: string, projectId: ProjectId, changes: Record<string, unknown> = {}) => ({
  id: ThreadId.make(id),
  projectId,
  title: `Title ${id}`,
  lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: ThreadId.make(id) },
  delegatedFrom: null,
  archivedAt: null,
  deletedAt: null,
  ...changes,
});
const index = new Map(
  [
    thread("t-assistant", game),
    thread("t-game", game),
    thread("t-archived", site, { archivedAt: "2026-11-27T10:00:00.000Z" }),
    thread("t-child", site, {
      lineage: {
        parentThreadId: ThreadId.make("t-game"),
        relationshipToParent: "subagent",
        rootThreadId: ThreadId.make("t-game"),
      },
    }),
  ].map((entry) => [entry.id, entry] as const),
);

describe("thread roles list (CAL4)", () => {
  it("lists nothing for an older server or no roles", () => {
    expect(threadRoleEntries(undefined, index)).toEqual([]);
    expect(threadRoleEntries({ assistant: null, orchestrators: {} }, index)).toEqual([]);
  });

  it("lists the assistant first, then each orchestrator, and marks each that holds no role", () => {
    const roles = {
      assistant: ThreadId.make("t-assistant"),
      orchestrators: {
        [game]: ThreadId.make("t-game"),
        [site]: ThreadId.make("t-archived"),
        [gone]: ThreadId.make("t-deleted"),
        // Hand-edited: a child, and a thread of another project.
        [ProjectId.make("project-child")]: ThreadId.make("t-child"),
        [ProjectId.make("project-other")]: ThreadId.make("t-assistant"),
      },
    } as ThreadRoles;
    expect(
      threadRoleEntries(roles, index).map((entry) => [
        entry.role,
        entry.projectId,
        entry.title,
        entry.problem,
      ]),
    ).toEqual([
      ["assistant", null, "Title t-assistant", null],
      ["orchestrator", game, "Title t-game", null],
      ["orchestrator", site, "Title t-archived", "archived"],
      ["orchestrator", gone, null, "missing"],
      ["orchestrator", "project-child", "Title t-child", "not-top-level"],
      ["orchestrator", "project-other", "Title t-assistant", "other-project"],
    ]);
  });
});
