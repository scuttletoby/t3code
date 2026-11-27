// A handoff write turn the fork marks stop-requested stays quiet in the browser whether it
// completes or fails (fork/handoff/handoffShell.ts marks it). The mocks follow
// ThreadNotificationCoordinator.badge.browser.test.ts.
import type { AnyRouter } from "@tanstack/react-router";
import * as DateTime from "effect/DateTime";
import * as Option from "effect/Option";
import { Atom, AtomRegistry } from "effect/reactivity";
import { expect, it, vi } from "vite-plus/test";

import { stubGlobal } from "../../../test/render";

const state = vi.hoisted(() => ({
  mode: "notifications",
  inApp: false,
  toast: vi.fn(),
  closeToast: vi.fn(),
  shells: null as unknown as { set: (id: string, value: unknown) => void },
  navigate: vi.fn(),
  sound: vi.fn(),
  badge: vi.fn(),
  settingsListeners: new Set<() => void>(),
}));
vi.mock(import("../ui/toast"), async (original) => {
  const actual = await original();
  return {
    ...actual,
    toastManager: {
      ...actual.toastManager,
      add: state.toast as never,
      close: state.closeToast as never,
    },
  };
});
vi.mock(import("../../state/shell"), async (original) => {
  const actual = await original();
  return {
    ...actual,
    environmentShell: {
      ...actual.environmentShell,
      stateValueAtom: ((id: string) => shellAtom(id)) as never,
    },
  };
});
vi.mock(import("../../state/presentation"), async (original) => {
  const actual = await original();
  return {
    ...actual,
    environmentSummaries: {
      ...actual.environmentSummaries,
      // Read lazily: the factory runs before this module's own bindings exist.
      get environmentIdsAtom() {
        return environmentIdsAtom as never;
      },
    },
  };
});
vi.mock(import("../../state/threads"), async (original) => {
  const actual = await original();
  return {
    ...actual,
    environmentThreadShells: {
      ...actual.environmentThreadShells,
      pendingSubagentRequestsAtom: (() => noRequestsAtom) as never,
      get delegatedUnderParentAtom() {
        return noDelegatedAtom as never;
      },
    },
  };
});
vi.mock(import("../../hooks/useSettings"), async (original) => ({
  ...(await original()),
  subscribeClientSettings: ((listener: () => void) => {
    state.settingsListeners.add(listener);
    return () => state.settingsListeners.delete(listener);
  }) as never,
  getClientSettings: (() => ({
    notificationMode: state.mode,
    inAppNotificationsEnabled: state.inApp,
  })) as never,
}));
vi.mock(import("../../threadNotifications"), async (original) => ({
  ...(await original()),
  playNotificationSound: state.sound,
  unlockNotificationAudio: vi.fn(),
  setNotificationBadge: state.badge,
}));

import { startThreadNotifications } from "../ThreadNotificationCoordinator";

let registry: AtomRegistry.AtomRegistry;
const shellAtom = Atom.family((_id: string) => Atom.make<unknown>(null));
const environmentIdsAtom = Atom.make<ReadonlyArray<string>>([]);
const noRequestsAtom = Atom.make(new Map());
const noDelegatedAtom = Atom.make<ReadonlySet<string>>(new Set<string>());
state.shells = { set: (id, value) => registry.set(shellAtom(id), value) };

class TestNotification extends EventTarget {
  static permission = "granted";
  static sent: TestNotification[] = [];
  close = vi.fn();
  constructor(
    readonly title: string,
    readonly options: NotificationOptions,
  ) {
    super();
    TestNotification.sent.push(this);
  }
}

const AT = DateTime.makeUnsafe("2026-09-13T07:00:00.000Z");

function threadShell(status: "running" | "completed" | "failed", latestRunStopRequested: boolean) {
  return {
    id: "thread",
    projectId: "project",
    title: "Example thread",
    providerInstanceId: "codex",
    modelSelection: { instanceId: "codex", model: "gpt-5.4" },
    runtimeMode: "full-access",
    interactionMode: "default",
    branch: null,
    worktreePath: null,
    activeProviderThreadId: null,
    lineage: { rootThreadId: "thread", parentThreadId: null, relationshipToParent: null },
    forkedFrom: null,
    createdBy: "user",
    creationSource: "web",
    latestRunId: "turn",
    activeRunId: null,
    status,
    pendingRuntimeRequest: null,
    latestVisibleMessage: null,
    latestUserMessageAt: null,
    hasActionableProposedPlan: false,
    itemCount: 0,
    visibleItemCount: 0,
    createdAt: AT,
    updatedAt: AT,
    latestRunRequestedAt: AT,
    latestRunStartedAt: AT,
    latestRunCompletedAt: status === "running" ? undefined : DateTime.add(AT, { seconds: 1 }),
    latestRunStopRequested,
    archivedAt: null,
    settledOverride: null,
    settledAt: null,
    lastVisitedAt: null,
    deletedAt: null,
  };
}

const live = (thread: ReturnType<typeof threadShell>) => ({
  status: "live",
  snapshot: Option.some({ threads: [thread] }),
});

function setup() {
  vi.clearAllMocks();
  const stack = new AsyncDisposableStack();
  registry = AtomRegistry.make();
  const testRegistry = registry;
  stack.defer(() => testRegistry.dispose());
  state.mode = "notifications-and-sound";
  state.settingsListeners.clear();
  registry.set(environmentIdsAtom, ["one"]);
  state.shells.set("one", live(threadShell("running", false)));
  TestNotification.sent = [];
  stack.use(stubGlobal("Notification", TestNotification));
  stack.use(vi.spyOn(document, "hasFocus").mockImplementation(() => false));
  stack.use(vi.spyOn(document, "visibilityState", "get").mockImplementation(() => "visible"));
  const router = {
    state: { matches: [{ params: {} }] },
    navigate: state.navigate,
    subscribe: () => () => undefined,
  } as unknown as AnyRouter;
  stack.defer(startThreadNotifications(testRegistry, router));
  return stack;
}

it.each([
  ["completed", false, ["Thread completed"]],
  ["failed", false, ["Thread failed"]],
  ["completed", true, []],
  ["failed", true, []],
] as const)(
  "a %s run marked stop-requested (%s) presents %j",
  async (status, latestRunStopRequested, titles) => {
    await using _app = setup();
    state.shells.set("one", live(threadShell(status, latestRunStopRequested)));
    expect(TestNotification.sent.map(({ title }) => title)).toEqual(titles);
    expect(state.sound).toHaveBeenCalledTimes(titles.length);
  },
);
