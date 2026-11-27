// Fork (calm sidebar): which rows the web sidebar's own list keeps in the calm view, and the group's
// heading. Each test names the question it answers.
import type { EnvironmentThreadShell } from "@t3tools/client-runtime/state/models";
import type { ResolvedThreadRoles } from "@t3tools/client-runtime/state/thread-roles";
import { EnvironmentId, ProjectId, ThreadId } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import { calmGroupText, calmHiddenKeys, calmSidebarView, calmThreadKey } from "./calmSidebar.logic";

const local = EnvironmentId.make("local");
const game = ProjectId.make("project-game");
const site = ProjectId.make("project-site");
const NOW = "2026-11-27T12:00:00.000Z";

const shell = (
  id: string,
  projectId: ProjectId,
  changes: Partial<Record<keyof EnvironmentThreadShell, unknown>> = {},
): EnvironmentThreadShell =>
  ({
    environmentId: local,
    id: ThreadId.make(id),
    projectId,
    title: id,
    lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: ThreadId.make(id) },
    delegatedFrom: null,
    archivedAt: null,
    deletedAt: null,
    settledOverride: null,
    snoozedUntil: null,
    snoozedAt: null,
    pinnedAt: null,
    hasPendingApprovals: false,
    hasPendingUserInput: false,
    pendingSubagentRequest: null,
    pendingGuestMessageCount: 0,
    ...changes,
  }) as unknown as EnvironmentThreadShell;

const roles = (
  assistant: string | null,
  orchestrators: Record<string, string> = {},
): ReadonlyMap<EnvironmentId, ResolvedThreadRoles> =>
  new Map([
    [
      local,
      {
        assistant: assistant === null ? null : ThreadId.make(assistant),
        orchestrators: new Map(
          Object.entries(orchestrators).map(([project, thread]) => [
            ProjectId.make(project),
            ThreadId.make(thread),
          ]),
        ),
      },
    ],
  ]);

const assistant = shell("assistant", site);
const gameLead = shell("game-lead", game);
const worker = shell("worker", game);
const waiting = shell("waiting", game, { hasPendingApprovals: true });
const settled = shell("settled", game, { settledOverride: "settled", hasPendingUserInput: true });
const snoozed = shell("snoozed", site, { snoozedUntil: "2026-11-28T00:00:00.000Z" });
const LIST = [worker, gameLead, waiting, assistant, settled, snoozed];

const view = (
  changes: Partial<Parameters<typeof calmSidebarView>[0]> = {},
): ReturnType<typeof calmSidebarView> =>
  calmSidebarView({
    visible: LIST,
    roles: roles("assistant", { [game]: "game-lead" }),
    enabled: true,
    searching: false,
    now: NOW,
    ...changes,
  });

describe("calm sidebar view", () => {
  it("calm off, or a search running: the sidebar's list is upstream's", () => {
    expect(view({ enabled: false })).toEqual({ kind: "off" });
    expect(view({ searching: true })).toEqual({ kind: "off" });
    expect(calmHiddenKeys(view({ searching: true }), false, null).size).toBe(0);
  });

  it("calm on with no role held: the whole list stays, with the hint", () => {
    const noRoles = view({ roles: new Map() });
    expect(noRoles).toEqual({ kind: "no-roles" });
    expect(calmHiddenKeys(noRoles, false, null).size).toBe(0);
  });

  it("role rows go on top, assistant first; the rest fold, snoozed and settled stay in their shelves", () => {
    const calm = view();
    if (calm.kind !== "calm") throw new Error("expected the calm view");
    expect(calm.roleRows.map((row) => [row.thread.id, row.role])).toEqual([
      ["assistant", "assistant"],
      ["game-lead", "orchestrator"],
    ]);
    expect(calm.folded.map((thread) => thread.id)).toEqual(["worker", "waiting"]);
  });

  it("the waiting count counts the folded threads that wait on the person, not the shelves'", () => {
    const calm = view();
    if (calm.kind !== "calm") throw new Error("expected the calm view");
    expect(calm.waitingCount).toBe(1);
    expect(calmGroupText(calm)).toEqual({ label: "Other threads (2)", waiting: "1 waiting" });
    const quiet = view({ visible: [assistant, worker] });
    if (quiet.kind !== "calm") throw new Error("expected the calm view");
    expect(calmGroupText(quiet)).toEqual({ label: "Other threads (1)", waiting: null });
  });

  it("a closed group leaves out the role rows and the folded threads, but never the open thread", () => {
    const hidden = calmHiddenKeys(view(), false, calmThreadKey(waiting));
    expect([...hidden].toSorted()).toEqual(
      [assistant, gameLead, worker].map(calmThreadKey).toSorted(),
    );
    expect(hidden.has(calmThreadKey(settled))).toBe(false);
    expect(hidden.has(calmThreadKey(snoozed))).toBe(false);
  });

  it("an open group leaves out only the role rows, which are drawn on top", () => {
    const hidden = calmHiddenKeys(view(), true, null);
    expect([...hidden].toSorted()).toEqual([assistant, gameLead].map(calmThreadKey).toSorted());
  });

  it("a snooze that has passed folds the thread like any other", () => {
    const woke = shell("woke", site, { snoozedUntil: "2026-11-27T11:00:00.000Z" });
    const calm = view({ visible: [assistant, woke] });
    if (calm.kind !== "calm") throw new Error("expected the calm view");
    expect(calm.folded.map((thread) => thread.id)).toEqual(["woke"]);
  });
});
