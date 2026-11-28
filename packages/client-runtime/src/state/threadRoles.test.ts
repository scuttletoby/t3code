import {
  EnvironmentId,
  ProjectId,
  ThreadId,
  type ThreadRoles,
  ThreadRolesFromSettingsFile,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";
import { Atom, AtomRegistry } from "effect/reactivity";
import { describe, expect, it } from "vite-plus/test";

import type { EnvironmentCatalogState } from "./connections.ts";
import {
  calmPartition,
  createThreadRolesAtom,
  isWaitingOnPerson,
  NO_THREAD_ROLES,
  type ResolvedThreadRoles,
  resolveThreadRoles,
  threadRole,
} from "./threadRoles.ts";

const local = EnvironmentId.make("local");
const linked = EnvironmentId.make("linked");
const game = ProjectId.make("project-game");
const site = ProjectId.make("project-site");

interface Row {
  readonly environmentId: EnvironmentId;
  readonly id: ThreadId;
  readonly projectId: ProjectId;
  readonly lineage: {
    readonly parentThreadId: ThreadId | null;
    readonly relationshipToParent: "fork" | "subagent" | null;
    readonly rootThreadId: ThreadId;
    readonly sideChat?: boolean;
  };
  readonly delegatedFrom: {
    readonly environmentId: EnvironmentId;
    readonly threadId: ThreadId;
    readonly title: string;
  } | null;
  readonly archivedAt: string | null;
  readonly deletedAt: string | null;
  readonly settledAt: string | null;
  readonly snoozedUntil: string | null;
  readonly hasPendingApprovals: boolean;
  readonly hasPendingUserInput: boolean;
  readonly pendingSubagentRequest: "approval" | "user_input" | null;
  readonly pendingGuestMessageCount: number;
}

const row = (id: string, projectId: ProjectId, changes: Partial<Row> = {}): Row => ({
  environmentId: local,
  id: ThreadId.make(id),
  projectId,
  lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: ThreadId.make(id) },
  delegatedFrom: null,
  archivedAt: null,
  deletedAt: null,
  settledAt: null,
  snoozedUntil: null,
  hasPendingApprovals: false,
  hasPendingUserInput: false,
  pendingSubagentRequest: null,
  pendingGuestMessageCount: 0,
  ...changes,
});

const child = (id: string, parent: Row, changes: Partial<Row> = {}): Row =>
  row(id, parent.projectId, {
    lineage: {
      parentThreadId: parent.id,
      relationshipToParent: "subagent",
      rootThreadId: parent.id,
    },
    ...changes,
  });

const index = (...threads: ReadonlyArray<Row>) =>
  new Map(threads.map((thread) => [thread.id, thread] as const));

const roles = (
  assistant: string | null,
  orchestrators: Record<string, string> = {},
): ThreadRoles => ({
  assistant: assistant === null ? null : ThreadId.make(assistant),
  orchestrators: Object.fromEntries(
    Object.entries(orchestrators).map(([projectId, threadId]) => [
      ProjectId.make(projectId),
      ThreadId.make(threadId),
    ]),
  ),
});

const byLocal = (resolved: ResolvedThreadRoles) => new Map([[local, resolved]]);

/** The calm view's rows as short text: role rows first, then the folded ones. */
const shown = (
  threads: ReadonlyArray<Row>,
  resolved: ReadonlyMap<EnvironmentId, ResolvedThreadRoles>,
) => {
  const partition = calmPartition(threads, resolved, true);
  if (partition.kind !== "calm") return { kind: partition.kind };
  return {
    kind: partition.kind,
    top: partition.roleRows.map(({ thread, role }) => `${role}:${thread.id}`),
    other: partition.otherThreads.map((thread) => String(thread.id)),
    waiting: partition.otherThreads.filter(isWaitingOnPerson).length,
  };
};

const decodeOldSettingsFile = Schema.decodeSync(
  Schema.Struct({ threadRoles: ThreadRolesFromSettingsFile }),
);

const orchestrator = row("orch", game);
const assistant = row("assistant", site);
const plain = row("plain", game);
const subagent = child("sub", orchestrator);
const sideChat = row("side", game, {
  lineage: {
    parentThreadId: orchestrator.id,
    relationshipToParent: null,
    rootThreadId: orchestrator.id,
    sideChat: true,
  },
});

describe("resolveThreadRoles", () => {
  it("reads no roles from no settings, an empty value or pointers to nothing", () => {
    const threads = index(orchestrator, plain);
    expect(resolveThreadRoles(undefined, threads)).toBe(NO_THREAD_ROLES);
    expect(resolveThreadRoles(null, threads)).toBe(NO_THREAD_ROLES);
    expect(resolveThreadRoles(roles(null), threads)).toBe(NO_THREAD_ROLES);
    expect(resolveThreadRoles(roles("gone", { [game]: "gone-too" }), threads)).toBe(
      NO_THREAD_ROLES,
    );
  });

  it("reads an old settings file (no threadRoles key) as no roles", () => {
    const decoded = decodeOldSettingsFile({});
    expect(resolveThreadRoles(decoded.threadRoles, index(orchestrator))).toBe(NO_THREAD_ROLES);
  });

  it("keeps pointers that name a live top-level thread of the right project", () => {
    const resolved = resolveThreadRoles(
      roles("assistant", { [game]: "orch" }),
      index(orchestrator, assistant),
    );
    expect(resolved.assistant).toBe(assistant.id);
    expect([...resolved.orchestrators]).toEqual([[game, orchestrator.id]]);
  });

  it("drops a bad hand-edited entry alone", () => {
    const threads = index(orchestrator, row("site-orch", site));
    const bad = {
      assistant: 42,
      orchestrators: { [game]: "orch", [site]: 7, "": "site-orch", " padded": "site-orch" },
    } as unknown as ThreadRoles;
    const resolved = resolveThreadRoles(bad, threads);
    expect(resolved.assistant).toBeNull();
    expect([...resolved.orchestrators]).toEqual([[game, orchestrator.id]]);
    expect(resolveThreadRoles("nonsense" as unknown as ThreadRoles, threads)).toBe(NO_THREAD_ROLES);
    expect(
      resolveThreadRoles({ assistant: null, orchestrators: [] } as unknown as ThreadRoles, threads),
    ).toBe(NO_THREAD_ROLES);
  });

  it.each([
    { name: "archived", thread: row("orch", game, { archivedAt: "2026-11-20T10:00:00.000Z" }) },
    { name: "deleted", thread: row("orch", game, { deletedAt: "2026-11-20T10:00:00.000Z" }) },
    { name: "a sub-agent", thread: child("orch", plain) },
    { name: "a side chat", thread: { ...sideChat, id: ThreadId.make("orch") } },
    {
      name: "a task delegated from a linked machine",
      thread: row("orch", game, {
        delegatedFrom: { environmentId: linked, threadId: ThreadId.make("p"), title: "p" },
      }),
    },
  ])("counts a pointer to a thread that is $name as no role", ({ thread }) => {
    expect(resolveThreadRoles(roles("orch", { [game]: "orch" }), index(thread))).toBe(
      NO_THREAD_ROLES,
    );
  });

  it("drops an orchestrator moved to another project, but keeps it as the assistant", () => {
    const moved = resolveThreadRoles(roles("orch", { [game]: "orch" }), index(row("orch", site)));
    expect(moved.assistant).toBe(orchestrator.id);
    expect(moved.orchestrators.size).toBe(0);
  });

  it("brings the role back when an archived thread is unarchived (the pointer is kept)", () => {
    const setting = roles(null, { [game]: "orch" });
    const archived = row("orch", game, { archivedAt: "2026-11-20T10:00:00.000Z" });
    expect(resolveThreadRoles(setting, index(archived))).toBe(NO_THREAD_ROLES);
    expect(resolveThreadRoles(setting, index(orchestrator)).orchestrators.get(game)).toBe(
      orchestrator.id,
    );
  });
});

describe("threadRole", () => {
  const resolved = resolveThreadRoles(
    roles("assistant", { [game]: "orch" }),
    index(orchestrator, assistant),
  );

  it("names each kind of thread", () => {
    expect(threadRole(assistant, resolved)).toBe("assistant");
    expect(threadRole(orchestrator, resolved)).toBe("orchestrator");
    expect(threadRole(plain, resolved)).toBe("plain");
    expect(threadRole(subagent, resolved)).toBe("child");
    expect(threadRole(sideChat, resolved)).toBe("child");
    expect(
      threadRole(
        row("task", game, {
          delegatedFrom: { environmentId: linked, threadId: ThreadId.make("p"), title: "p" },
        }),
        resolved,
      ),
    ).toBe("child");
    expect(threadRole(orchestrator, undefined)).toBe("plain");
  });

  it("makes a fork of the orchestrator plain (a new thread id, no second orchestrator)", () => {
    const fork = row("orch-fork", game, {
      lineage: {
        parentThreadId: orchestrator.id,
        relationshipToParent: "fork",
        rootThreadId: orchestrator.id,
      },
    });
    expect(threadRole(fork, resolved)).toBe("plain");
  });

  it("checks the row again: roles read before the row was archived or moved lift nothing", () => {
    expect(threadRole({ ...orchestrator, archivedAt: "2026-11-21T00:00:00.000Z" }, resolved)).toBe(
      "plain",
    );
    expect(threadRole({ ...orchestrator, deletedAt: "2026-11-21T00:00:00.000Z" }, resolved)).toBe(
      "plain",
    );
    expect(threadRole({ ...orchestrator, projectId: site }, resolved)).toBe("plain");
  });

  it("keeps a thread that is both assistant and orchestrator as the assistant", () => {
    const both = resolveThreadRoles(roles("orch", { [game]: "orch" }), index(orchestrator));
    expect(threadRole(orchestrator, both)).toBe("assistant");
  });
});

describe("calmPartition", () => {
  const list = [plain, orchestrator, assistant, row("site-plain", site)];
  const resolved = byLocal(
    resolveThreadRoles(roles("assistant", { [game]: "orch" }), index(...list)),
  );

  it("is upstream's list, the same array, while calm is off", () => {
    expect(calmPartition(list, resolved, false)).toEqual({ kind: "off", threads: list });
    expect((calmPartition(list, resolved, false) as { threads: unknown }).threads).toBe(list);
  });

  it("puts the assistant, then orchestrators, on top and folds the rest in list order", () => {
    expect(shown(list, resolved)).toEqual({
      kind: "calm",
      top: ["assistant:assistant", "orchestrator:orch"],
      other: ["plain", "site-plain"],
      waiting: 0,
    });
  });

  it("shows the normal list (with the hint) when no listed thread holds a role", () => {
    expect(calmPartition(list, new Map(), true)).toEqual({ kind: "no-roles", threads: list });
    const toArchived = byLocal(
      resolveThreadRoles(
        roles(null, { [game]: "orch" }),
        index({ ...orchestrator, archivedAt: "2026-11-20T10:00:00.000Z" }),
      ),
    );
    expect(calmPartition([plain], toArchived, true).kind).toBe("no-roles");
  });

  it("gives an empty list without the hint for a new install with no threads", () => {
    expect(calmPartition([], resolved, true)).toEqual({ kind: "off", threads: [] });
    expect(calmPartition([], new Map(), true)).toEqual({ kind: "off", threads: [] });
    expect(calmPartition([], new Map(), false)).toEqual({ kind: "off", threads: [] });
  });

  it("returns every thread exactly once and leaves the input as it was", () => {
    const input = [...list];
    const partition = calmPartition(input, resolved, true);
    if (partition.kind !== "calm") throw new Error("expected calm");
    const ids = [
      ...partition.roleRows.map(({ thread }) => thread.id),
      ...partition.otherThreads.map((thread) => thread.id),
    ];
    expect(ids.toSorted()).toEqual(list.map((thread) => thread.id).toSorted());
    expect(input).toEqual(list);
  });

  it("folds a project with no orchestrator into the group, with no error", () => {
    const sitePlain = row("site-plain", site);
    expect(shown([orchestrator, sitePlain], resolved).other).toEqual(["site-plain"]);
  });

  it("keeps a settled or snoozed role thread on top", () => {
    const settled = { ...orchestrator, settledAt: "2026-11-20T10:00:00.000Z" };
    const snoozed = { ...assistant, snoozedUntil: "2099-01-01T00:00:00.000Z" };
    expect(shown([settled, snoozed, plain], resolved).top).toEqual([
      "assistant:assistant",
      "orchestrator:orch",
    ]);
  });

  it("shows an assistant and an orchestrator of the same project both on top", () => {
    const helper = row("helper", game);
    const sameProject = byLocal(
      resolveThreadRoles(roles("helper", { [game]: "orch" }), index(orchestrator, helper)),
    );
    expect(shown([orchestrator, helper, plain], sameProject).top).toEqual([
      "assistant:helper",
      "orchestrator:orch",
    ]);
  });

  it("lists a thread that holds two roles once, as the assistant", () => {
    const both = byLocal(
      resolveThreadRoles(roles("orch", { [game]: "orch" }), index(orchestrator)),
    );
    expect(shown([orchestrator, plain], both)).toEqual({
      kind: "calm",
      top: ["assistant:orch"],
      other: ["plain"],
      waiting: 0,
    });
  });

  it("moves the row on a handoff: the new thread on top, the old one folded", () => {
    const next = row("orch-2", game);
    const threads = index(orchestrator, next, plain);
    const before = byLocal(resolveThreadRoles(roles(null, { [game]: "orch" }), threads));
    const after = byLocal(resolveThreadRoles(roles(null, { [game]: "orch-2" }), threads));
    expect(shown([orchestrator, next, plain], before).top).toEqual(["orchestrator:orch"]);
    expect(shown([orchestrator, next, plain], after)).toEqual({
      kind: "calm",
      top: ["orchestrator:orch-2"],
      other: ["orch", "plain"],
      waiting: 0,
    });
  });

  it("counts folded threads waiting on the person, not role rows", () => {
    const waitingOrchestrator = { ...orchestrator, hasPendingApprovals: true };
    const threads = [
      waitingOrchestrator,
      row("approval", game, { hasPendingApprovals: true }),
      row("question", game, { hasPendingUserInput: true }),
      row("child-approval", game, { pendingSubagentRequest: "approval" }),
      row("guest", game, { pendingGuestMessageCount: 2 }),
      plain,
    ];
    expect(shown(threads, resolved)).toMatchObject({
      top: ["orchestrator:orch"],
      waiting: 4,
    });
  });

  it("folds a delegated task whose parent is archived, and counts it when it waits", () => {
    // Upstream lists such a task on its own again; it can never hold a role.
    const task = row("task", game, {
      environmentId: linked,
      delegatedFrom: { environmentId: local, threadId: orchestrator.id, title: "o" },
      hasPendingApprovals: true,
    });
    const pointsAtTask = new Map([
      [local, resolved.get(local)!],
      [linked, resolveThreadRoles(roles("task"), index(task))],
    ]);
    expect(shown([orchestrator, task], pointsAtTask)).toEqual({
      kind: "calm",
      top: ["orchestrator:orch"],
      other: ["task"],
      waiting: 1,
    });
  });

  it("keeps each server's roles to its own threads", () => {
    const sameIdElsewhere = { ...orchestrator, environmentId: linked };
    expect(shown([orchestrator, sameIdElsewhere], resolved)).toEqual({
      kind: "calm",
      top: ["orchestrator:orch"],
      other: ["orch"],
      waiting: 0,
    });
  });
});

it("isWaitingOnPerson follows the sidebar's approval, input and guest pills", () => {
  expect(isWaitingOnPerson(plain)).toBe(false);
  expect(isWaitingOnPerson({ ...plain, pendingSubagentRequest: "user_input" })).toBe(true);
  expect(isWaitingOnPerson({ hasPendingApprovals: false, hasPendingUserInput: false })).toBe(false);
});

describe("createThreadRolesAtom", () => {
  type Settings = { readonly threadRoles?: ThreadRoles } | null;

  const catalog = (entries: ReadonlyArray<[EnvironmentId, boolean]>): EnvironmentCatalogState => ({
    isReady: true,
    entries: new Map(
      entries.map(([environmentId, enabled]) => [
        environmentId,
        { enabled } as unknown as EnvironmentCatalogState["entries"] extends ReadonlyMap<
          unknown,
          infer Entry
        >
          ? Entry
          : never,
      ]),
    ),
  });

  const setup = () => {
    const catalogAtom = Atom.make(
      catalog([
        [local, true],
        [linked, true],
      ]),
    );
    const settingsAtoms = new Map([
      [local, Atom.make<Settings>(null)],
      [linked, Atom.make<Settings>(null)],
    ]);
    const threadAtoms = new Map([
      [local, Atom.make(index(orchestrator, assistant, plain))],
      [linked, Atom.make(index({ ...plain, environmentId: linked, id: ThreadId.make("l") }))],
    ]);
    let threadReads = 0;
    const rolesAtom = createThreadRolesAtom({
      catalogValueAtom: catalogAtom,
      settingsValueAtom: (environmentId) => settingsAtoms.get(environmentId)!,
      environmentThreadIndexAtom: (environmentId) =>
        Atom.make((get) => {
          threadReads += 1;
          return get(threadAtoms.get(environmentId)!);
        }),
    });
    const registry = AtomRegistry.make();
    return {
      registry,
      rolesAtom,
      catalogAtom,
      settingsAtoms,
      threadAtoms,
      threadReads: () => threadReads,
    };
  };

  it("is empty for servers without the setting (older servers) or not loaded yet", () => {
    const { registry, rolesAtom, settingsAtoms } = setup();
    registry.set(settingsAtoms.get(local)!, {});
    expect(registry.get(rolesAtom).size).toBe(0);
    registry.dispose();
  });

  it("reads no threads while no server names a role", () => {
    const { registry, rolesAtom, settingsAtoms, threadReads } = setup();
    registry.set(settingsAtoms.get(local)!, { threadRoles: roles(null) });
    registry.get(rolesAtom);
    expect(threadReads()).toBe(0);
    registry.dispose();
  });

  it("follows a settings change while the list is open", () => {
    const { registry, rolesAtom, settingsAtoms } = setup();
    const seen: Array<ReadonlyMap<EnvironmentId, ResolvedThreadRoles>> = [];
    const stop = registry.subscribe(rolesAtom, (value) => seen.push(value), { immediate: true });
    registry.set(settingsAtoms.get(local)!, { threadRoles: roles(null, { [game]: "orch" }) });
    registry.set(settingsAtoms.get(local)!, {
      threadRoles: roles("assistant", { [game]: "orch" }),
    });
    registry.set(settingsAtoms.get(local)!, { threadRoles: roles(null) });
    expect(
      seen.map((value) => ({
        assistant: value.get(local)?.assistant ?? null,
        orchestrator: value.get(local)?.orchestrators.get(game) ?? null,
      })),
    ).toEqual([
      { assistant: null, orchestrator: null },
      { assistant: null, orchestrator: orchestrator.id },
      { assistant: assistant.id, orchestrator: orchestrator.id },
      { assistant: null, orchestrator: null },
    ]);
    stop();
    registry.dispose();
  });

  it("keeps the same map when the same roles arrive again (a resave or a server restart)", () => {
    const { registry, rolesAtom, settingsAtoms, threadAtoms } = setup();
    registry.set(settingsAtoms.get(local)!, { threadRoles: roles(null, { [game]: "orch" }) });
    const first = registry.get(rolesAtom);
    registry.set(settingsAtoms.get(local)!, { threadRoles: roles(null, { [game]: "orch" }) });
    registry.set(threadAtoms.get(local)!, index(orchestrator, assistant, plain, row("new", game)));
    expect(registry.get(rolesAtom)).toBe(first);
    registry.dispose();
  });

  it("drops the role when its thread is archived and brings it back on unarchive", () => {
    const { registry, rolesAtom, settingsAtoms, threadAtoms } = setup();
    registry.set(settingsAtoms.get(local)!, { threadRoles: roles(null, { [game]: "orch" }) });
    registry.set(
      threadAtoms.get(local)!,
      index({ ...orchestrator, archivedAt: "2026-11-20T10:00:00.000Z" }, plain),
    );
    expect(registry.get(rolesAtom).has(local)).toBe(false);
    registry.set(threadAtoms.get(local)!, index(orchestrator, plain));
    expect(registry.get(rolesAtom).get(local)?.orchestrators.get(game)).toBe(orchestrator.id);
    registry.dispose();
  });

  it("drops a server that goes offline or is turned off, and brings it back with it", () => {
    const { registry, rolesAtom, settingsAtoms, catalogAtom } = setup();
    registry.set(settingsAtoms.get(local)!, { threadRoles: roles(null, { [game]: "orch" }) });
    registry.set(settingsAtoms.get(linked)!, { threadRoles: roles("l") });
    expect([...registry.get(rolesAtom).keys()]).toEqual([local, linked]);
    registry.set(
      catalogAtom,
      catalog([
        [local, true],
        [linked, false],
      ]),
    );
    expect([...registry.get(rolesAtom).keys()]).toEqual([local]);
    registry.set(
      catalogAtom,
      catalog([
        [local, true],
        [linked, true],
      ]),
    );
    expect(registry.get(rolesAtom).get(linked)?.assistant).toBe(ThreadId.make("l"));
    registry.dispose();
  });
});
