// The calm sidebar's web rules (CAL3, CAL4): what the calm view draws on top and which of
// upstream's list items it hides; and the thread menu's "Thread role" entry and its saves.
import { resolveThreadRoles } from "@t3tools/client-runtime/state/thread-roles";
import { EnvironmentId, ProjectId, ThreadId } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  buildThreadActionMenuItems,
  type ThreadActionMenuState,
} from "../../threadActionMenu.logic";
import { calmSkipsListItem, calmView, otherThreadsAccessory } from "./calmSidebar.logic";
import {
  ROLE_MAKE_ASSISTANT,
  ROLE_MAKE_ORCHESTRATOR,
  ROLE_REMOVE,
  roleMenuItems,
  roleMenuPatch,
} from "./roleMenu.logic";

const env = EnvironmentId.make("local");
const game = ProjectId.make("project-game");
const site = ProjectId.make("project-site");

const row = (id: string, projectId = game, changes: Record<string, unknown> = {}) => ({
  environmentId: env,
  id: ThreadId.make(id),
  projectId,
  lineage: {
    parentThreadId: null,
    relationshipToParent: null,
    rootThreadId: ThreadId.make(id),
  } as const,
  delegatedFrom: null,
  archivedAt: null,
  deletedAt: null,
  hasPendingApprovals: false,
  hasPendingUserInput: false,
  pendingSubagentRequest: null,
  pendingGuestMessageCount: 0,
  ...changes,
});
type Row = ReturnType<typeof row>;
const keyOf = (thread: Row) => `${thread.environmentId}:${thread.id}`;

const threads = [
  row("t-plain"),
  row("t-orch-game"),
  row("t-asks", site, { hasPendingApprovals: true }),
  row("t-assistant", site),
];
const rolesOf = (pointers: { assistant?: string; orchestrators?: Record<string, string> }) =>
  new Map([
    [
      env,
      resolveThreadRoles(
        {
          assistant: (pointers.assistant ?? null) as ThreadId | null,
          orchestrators: (pointers.orchestrators ?? {}) as Record<ProjectId, ThreadId>,
        },
        new Map(threads.map((thread) => [thread.id, thread])),
      ),
    ],
  ]);
const ROLES = rolesOf({ assistant: "t-assistant", orchestrators: { [game]: "t-orch-game" } });

const view = (changes: Partial<Parameters<typeof calmView<Row>>[0]> = {}) =>
  calmView<Row>({
    threads,
    rolesByEnvironment: ROLES,
    enabled: true,
    open: false,
    routeRowThreadKey: null,
    keyOf,
    ...changes,
  });

describe("calm view (CAL3)", () => {
  it("is off while the switch is off, and for an empty list", () => {
    expect(view({ enabled: false }).kind).toBe("off");
    expect(view({ threads: [] }).kind).toBe("off");
  });

  it("gives the hint, and hides nothing, while no listed thread holds a role", () => {
    const hint = view({ rolesByEnvironment: new Map() });
    expect(hint.kind).toBe("hint");
    expect(
      calmSkipsListItem(hint, { kind: "thread", key: "local:t-plain", section: "active" }),
    ).toBe(false);
  });

  it("puts the assistant, then the orchestrators, on top; counts the rest and who waits", () => {
    const calm = view();
    if (calm.kind !== "calm") throw new Error(calm.kind);
    expect(calm.roleRows.map((entry) => [entry.thread.id, entry.role])).toEqual([
      ["t-assistant", "assistant"],
      ["t-orch-game", "orchestrator"],
    ]);
    expect(calm.otherCount).toBe(2);
    expect(calm.waitingCount).toBe(1);
    expect(otherThreadsAccessory(calm)).toBe("1 waiting · 2");
    expect(otherThreadsAccessory({ otherCount: 2, waitingCount: 0 })).toBe("2");
  });

  it("closed: hides every list item and keeps the open thread in sight", () => {
    const closed = view({ routeRowThreadKey: "local:t-plain" });
    if (closed.kind !== "calm") throw new Error(closed.kind);
    expect(closed.routeOther?.id).toBe("t-plain");
    for (const item of [
      { kind: "thread", key: "local:t-plain", section: "active" },
      { kind: "thread", key: "local:t-assistant", section: "settled" },
      { kind: "marker", marker: "settled-header" },
    ] as const) {
      expect(calmSkipsListItem(closed, item)).toBe(true);
    }
    // A role thread that is open is already on top: not drawn twice.
    const onRole = view({ routeRowThreadKey: "local:t-assistant" });
    expect(onRole.kind === "calm" && onRole.routeOther).toBe(null);
  });

  it("open: hides only the role rows, which are drawn on top", () => {
    const open = view({ open: true, routeRowThreadKey: "local:t-plain" });
    if (open.kind !== "calm") throw new Error(open.kind);
    expect(open.routeOther).toBe(null);
    expect(
      calmSkipsListItem(open, { kind: "thread", key: "local:t-orch-game", section: "active" }),
    ).toBe(true);
    expect(
      calmSkipsListItem(open, { kind: "thread", key: "local:t-plain", section: "active" }),
    ).toBe(false);
    expect(calmSkipsListItem(open, { kind: "marker", marker: "settled-header" })).toBe(false);
  });

  it("a pointer to an archived thread gives no role row", () => {
    const archived = [
      ...threads.slice(0, 3),
      row("t-assistant", site, { archivedAt: "2026-11-27" }),
    ];
    const calm = calmView<Row>({
      threads: archived.filter((thread) => thread.archivedAt === null),
      rolesByEnvironment: ROLES,
      enabled: true,
      open: false,
      routeRowThreadKey: null,
      keyOf,
    });
    expect(calm.kind === "calm" && calm.roleRows.map((entry) => entry.thread.id)).toEqual([
      "t-orch-game",
    ]);
  });
});

const MENU_BASE: ThreadActionMenuState = {
  canOperate: true,
  branch: null,
  projectFilter: null,
  isPinned: false,
  isSettled: false,
  autoSettleEnabled: true,
  isSnoozed: false,
  canSnoozeNow: true,
  isRegeneratingTitle: false,
  isRunning: false,
  supports: {
    settlement: true,
    autoSettleOptOut: false,
    snooze: false,
    pinning: false,
    titleRegeneration: false,
  },
  snoozePresets: [],
};
const NO_ROLE = {
  isAssistant: false,
  isOrchestrator: false,
  assistantElsewhere: false,
  orchestratorElsewhere: false,
};
const roleEntry = (state: ThreadActionMenuState) =>
  buildThreadActionMenuItems(state).find((item) => (item.id as string) === "fork-role");

describe("thread menu: Thread role (CAL4)", () => {
  it("is in the thread menu only when the caller gives its state", () => {
    expect(roleEntry(MENU_BASE)).toBeUndefined();
    expect(roleMenuItems(undefined)).toEqual([]);
    expect(roleEntry({ ...MENU_BASE, forkThreadRole: NO_ROLE })?.label).toBe("Thread role");
  });

  it("makes: says when it replaces another thread, and offers no remove without a role", () => {
    const make = roleEntry({ ...MENU_BASE, forkThreadRole: NO_ROLE })!.children!;
    expect(make.map((item) => item.label)).toEqual(["Make assistant", "Make project orchestrator"]);
    const replace = roleEntry({
      ...MENU_BASE,
      forkThreadRole: { ...NO_ROLE, assistantElsewhere: true, orchestratorElsewhere: true },
    })!.children!;
    expect(replace.map((item) => item.label)).toEqual([
      "Make assistant (replaces the current one)",
      "Make project orchestrator (replaces the current one)",
    ]);
  });

  it("marks the role the thread holds and offers to remove it", () => {
    const held = roleEntry({
      ...MENU_BASE,
      forkThreadRole: { ...NO_ROLE, isOrchestrator: true },
    })!.children!;
    expect(held.map((item) => [item.id, item.checked ?? false, item.disabled ?? false])).toEqual([
      [ROLE_MAKE_ASSISTANT, false, false],
      [ROLE_MAKE_ORCHESTRATOR, true, true],
      [ROLE_REMOVE, false, false],
    ]);
  });

  it("is greyed out for a connection that cannot change threads", () => {
    const entry = roleEntry({ ...MENU_BASE, canOperate: false, forkThreadRole: NO_ROLE })!;
    expect(entry.disabled).toBe(true);
    expect(entry.children!.every((item) => item.disabled)).toBe(true);
  });

  it("saves only the entry it changes", () => {
    const thread = { id: ThreadId.make("t-1"), projectId: game };
    expect(roleMenuPatch(ROLE_MAKE_ASSISTANT, thread, NO_ROLE)).toEqual({ assistant: "t-1" });
    expect(roleMenuPatch(ROLE_MAKE_ORCHESTRATOR, thread, NO_ROLE)).toEqual({
      orchestrators: { [game]: "t-1" },
    });
    expect(roleMenuPatch(ROLE_REMOVE, thread, { isAssistant: true, isOrchestrator: true })).toEqual(
      { assistant: null, orchestrators: { [game]: null } },
    );
    expect(
      roleMenuPatch(ROLE_REMOVE, thread, { isAssistant: false, isOrchestrator: true }),
    ).toEqual({ orchestrators: { [game]: null } });
    expect(roleMenuPatch(ROLE_REMOVE, thread, NO_ROLE)).toBeNull();
  });
});
