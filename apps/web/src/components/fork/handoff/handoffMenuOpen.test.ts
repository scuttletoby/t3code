/**
 * Fork (HO5): the thread menu hook's state read before it opens (useThreadActionMenu.openMenu).
 * Off (the default) opens at once, with no read; on, it waits for the read, at most its limit.
 */
import {
  AuthOrchestrationOperateScope,
  DEFAULT_HANDOFF_SETTINGS,
  DEFAULT_SERVER_SETTINGS,
  EnvironmentId,
  ThreadId,
  type ContextMenuItem,
  type HandoffRouteState,
  type ServerSettings,
} from "@t3tools/contracts";
import { AsyncResult } from "effect/reactivity";
import { afterEach, beforeEach, describe, expect, it, vi } from "vite-plus/test";

import type { ThreadActionMenuId } from "../../threadActionMenu.logic";

const state = vi.hoisted(() => ({
  settings: null as ServerSettings | null,
  read: vi.fn<(threadRef: unknown) => Promise<HandoffRouteState | null>>(),
  show: vi.fn<
    (
      items: ReadonlyArray<ContextMenuItem<ThreadActionMenuId>>,
    ) => Promise<ThreadActionMenuId | null>
  >(),
}));

vi.mock("./handoffApi", () => ({
  readHandoffState: state.read,
  askHandoffNow: vi.fn(),
}));
vi.mock("../../../rpc/atomRegistry", () => ({
  appAtomRegistry: {
    get: () => (state.settings === null ? null : { settings: state.settings }),
  },
}));
vi.mock("../../../state/server", () => ({ primaryServerConfigAtom: "primary-config" }));
vi.mock("../../CustomSnoozeDialog", () => ({ requestCustomSnooze: vi.fn() }));
vi.mock("react", () => ({
  useCallback: (callback: unknown) => callback,
  useMemo: (factory: () => unknown) => factory(),
}));
vi.mock("@tanstack/react-router", () => ({ useRouter: () => ({ navigate: async () => {} }) }));
vi.mock("../../../state/session", () => ({
  readEnvironmentScope: (_environmentId: string, scope: string) =>
    scope === AuthOrchestrationOperateScope,
  readEnvironmentScopeDenied: () => false,
}));
vi.mock("../../../state/entities", () => ({
  readEnvironmentServerConfigLoaded: () => true,
  readEnvironmentSupportsAutoSettleOptOut: () => true,
  readThreadShell: () => ({
    id: "thread",
    environmentId: "primary",
    projectId: "project",
    title: "Thread",
    branch: "main",
    worktreePath: null,
    runtime: null,
    latestRun: null,
  }),
  useProjects: () => [{ id: "project", environmentId: "primary" }],
}));
vi.mock("../../../state/environments", () => ({ usePrimaryEnvironmentId: () => "primary" }));
vi.mock("../../../state/threads", () => ({ threadEnvironment: { updateMetadata: "metadata" } }));
vi.mock("../../../state/use-atom-command", () => ({
  useAtomCommand: () => async () => AsyncResult.success(undefined),
}));
vi.mock("../../../localApi", () => ({
  readLocalApi: () => ({
    contextMenu: { show: state.show, close: () => {} },
    dialogs: { confirm: async () => false },
  }),
}));
vi.mock("@t3tools/client-runtime/state/project-grouping", () => ({
  deriveLogicalProjectKeyFromSettings: () => "project",
  derivePhysicalProjectKey: () => "project",
  selectProjectGroupingSettings: () => ({}),
}));
vi.mock("../../../sidebarProjectGrouping", () => ({
  buildPhysicalToLogicalProjectKeyMap: () => new Map(),
}));
vi.mock("../../../uiStateStore", () => ({
  useUiStateStore: (select: (store: unknown) => unknown) => select({ markThreadUnread: () => {} }),
}));
vi.mock("../../ui/toast", () => ({
  stackedThreadToast: (toast: unknown) => toast,
  toastManager: { add: () => {} },
}));
vi.mock("../../Sidebar.snooze", () => ({
  resolveSnoozePresets: () => [],
  snoozeWakeDescription: () => "later",
}));
vi.mock("../../../hooks/useCopyToClipboard", () => ({
  useCopyToClipboard: () => ({ copyToClipboard: () => {} }),
}));
vi.mock("../../../hooks/useHandleNewThread", () => ({ useNewThreadHandler: () => async () => {} }));
vi.mock("../../../hooks/useSettings", () => ({
  useClientSettings: (select: (settings: unknown) => unknown) =>
    select({ confirmThreadDelete: true, confirmThreadArchive: true, timestampFormat: "12-hour" }),
}));
vi.mock("../../../hooks/useThreadActions", () => ({
  canTurnThreadIntoProject: () => false,
  useThreadActions: () => new Proxy({}, { get: () => async () => AsyncResult.success(undefined) }),
}));

import { useThreadActionMenu } from "../../../hooks/useThreadActionMenu";

const openMenu = () =>
  useThreadActionMenu({
    threadRef: { environmentId: EnvironmentId.make("primary"), threadId: ThreadId.make("thread") },
    projectCwd: "/project",
    onStartRename: () => {},
    canShareWithGuest: false,
  }).openMenu({ x: 10, y: 20 });

const withHandoff = (handoff: Partial<ServerSettings["handoff"]>, roles?: string): ServerSettings =>
  ({
    ...DEFAULT_SERVER_SETTINGS,
    handoff: { ...DEFAULT_HANDOFF_SETTINGS, ...handoff },
    ...(roles === undefined
      ? {}
      : { threadRoles: { ...DEFAULT_SERVER_SETTINGS.threadRoles, assistant: roles } }),
  }) as ServerSettings;
const shownIds = () => state.show.mock.calls[0]![0].map((item) => item.id);

beforeEach(async () => {
  // The menu's settings check loads with its module, as in the app.
  await vi.dynamicImportSettled();
  vi.useFakeTimers();
  state.settings = DEFAULT_SERVER_SETTINGS;
  state.read.mockReset().mockReturnValue(new Promise(() => {}));
  state.show.mockReset().mockResolvedValue(null);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("Hand off now in the thread menu hook (HO5)", () => {
  it("off (the default) opens at once, with no read", async () => {
    openMenu();
    // Promises only: no time passes.
    await vi.advanceTimersByTimeAsync(0);
    expect(state.show).toHaveBeenCalledOnce();
    expect(state.read).not.toHaveBeenCalled();
    expect(shownIds()).not.toContain("fork:hand-off-now");
  });

  it.each([
    ["on for the server", withHandoff({ mode: "handoff" })],
    ["on for the thread's role", withHandoff({ roles: "handoff" }, "thread")],
    ["not known yet", null],
  ])("%s: reads first and shows the item", async (_name, settings) => {
    state.settings = settings;
    state.read.mockResolvedValue({
      on: true,
      usedTokens: 100_000,
      ask: { kind: "started" },
      waitingOn: [],
    } as unknown as HandoffRouteState);
    openMenu();
    await vi.advanceTimersByTimeAsync(0);
    expect(state.read).toHaveBeenCalledOnce();
    expect(shownIds()).toContain("fork:hand-off-now");
  });

  it("on: a read that does not answer opens without the item at the limit", async () => {
    state.settings = withHandoff({ mode: "handoff" });
    openMenu();
    await vi.advanceTimersByTimeAsync(299);
    expect(state.show).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(state.show).toHaveBeenCalledOnce();
    expect(shownIds()).not.toContain("fork:hand-off-now");
  });
});
