import {
  AuthOrchestrationOperateScope,
  EnvironmentId,
  ProjectId,
  ThreadId,
  type AuthEnvironmentScope,
  type EditorId,
  type ThreadPullRequestLink,
} from "@t3tools/contracts";
import { AsyncResult } from "effect/reactivity";
import { expect, it, vi } from "vite-plus/test";

import { act, fakeTimers, render } from "../../test/render";
import type { RemoteOpenState } from "../remoteOpen";
import { useRightPanelStore } from "../rightPanelStore";

const state = vi.hoisted(() => ({
  allowed: true,
  listeners: new Set<() => void>(),
  openEditor: vi.fn(),
  link: vi.fn(),
  unlink: vi.fn(),
  search: vi.fn(),
  choose: vi.fn(),
  openFile: vi.fn(),
  openExternal: vi.fn(),
  copy: vi.fn(),
  openRemote: vi.fn(),
  setMaximized: vi.fn(),
  remote: { mode: "local-exec" } as RemoteOpenState,
  pullRequests: [] as ThreadPullRequestLink[],
}));

vi.mock(import("../state/use-atom-query-runner"), async (original) => ({
  ...(await original()),
  useAtomQueryRunner: (() => state.search) as never,
}));
vi.mock(import("../state/use-atom-command"), async (original) => {
  const { threadEnvironment } = await import("../state/threads");
  return {
    ...(await original()),
    useAtomCommand: ((command: unknown) =>
      command === threadEnvironment.linkPullRequest
        ? state.link
        : command === threadEnvironment.unlinkPullRequest
          ? state.unlink
          : state.openEditor) as never,
  };
});
vi.mock(import("../state/session"), async (original) => {
  const { useSyncExternalStore } = await import("react");
  const readEnvironmentScope = (id: EnvironmentId | null, scope: AuthEnvironmentScope) =>
    id !== null &&
    (scope !== AuthOrchestrationOperateScope || id !== threadRef.environmentId || state.allowed);
  return {
    ...(await original()),
    readEnvironmentScope,
    useEnvironmentScope: (id: EnvironmentId | null, scope: AuthEnvironmentScope) =>
      useSyncExternalStore(
        (listener) => {
          state.listeners.add(listener);
          return () => state.listeners.delete(listener);
        },
        () => readEnvironmentScope(id, scope),
      ),
  };
});
vi.mock(import("../state/server"), async (original) => {
  const actual = await original();
  const { Atom } = await import("effect/reactivity");
  const config = Atom.make(() => serverConfig);
  return {
    ...actual,
    serverEnvironment: { ...actual.serverEnvironment, configValueAtom: (() => config) as never },
    environmentServerConfigsAtom: Atom.make(
      () => new Map([[threadRef.environmentId, serverConfig]]),
    ) as never,
  };
});
vi.mock(import("../state/entities"), async (original) => ({
  ...(await original()),
  readThreadShell: (() => ({ pullRequests: state.pullRequests })) as never,
  useProjects: (() => [{ id: projectId, environmentId: threadRef.environmentId }]) as never,
}));
vi.mock(import("../remoteOpen"), async (original) => ({
  ...(await original()),
  useRemoteOpenResolution: () => ({ state: state.remote, isResolved: true }) as never,
  useRemoteCapableEditors: () => ["vscode"],
  openRemoteEditorUrl: state.openRemote,
}));
vi.mock(import("../hooks/useLocalStorage"), async (original) => ({
  ...(await original()),
  useLocalStorage: (() => ["vscode", vi.fn()]) as never,
  getLocalStorageItem: (() => "vscode") as never,
}));
vi.mock(import("../localApi"), async (original) => ({
  ...(await original()),
  readLocalApi: () =>
    ({
      contextMenu: { show: state.choose },
      shell: { openExternal: state.openExternal },
    }) as never,
}));
vi.mock(import("@t3tools/client-runtime/change-request-links"), async (original) => ({
  ...(await original()),
  resolvePullRequestPreviewTarget: () => null,
  findProjectForChangeRequest: ((projects: readonly { id: ProjectId }[]) => projects[0]) as never,
}));

import ChatMarkdown from "./ChatMarkdown";

const threadRef = {
  environmentId: EnvironmentId.make("selected"),
  threadId: ThreadId.make("thread"),
};
const projectId = ProjectId.make("project");
const linkedPullRequest: ThreadPullRequestLink = {
  host: "github.com",
  repository: "example/repo",
  number: 42,
  url: "https://github.com/example/repo/pull/42",
  source: "manual",
  linkedAt: "2026-09-08T00:00:00.000Z",
  snapshot: null,
  stack: null,
};
const serverConfig = {
  availableEditors: ["vscode", "file-manager"] as readonly EditorId[],
  shellRevealInFileManager: true,
  shellRevealInFileManagerKind: "xdg-open",
  environment: { platform: { os: "linux" }, capabilities: {} },
};

function setup() {
  using stack = new DisposableStack();
  state.allowed = true;
  state.listeners.clear();
  state.pullRequests = [];
  state.openEditor.mockReset().mockResolvedValue(AsyncResult.success(undefined));
  state.link.mockReset().mockResolvedValue(AsyncResult.success(undefined));
  state.unlink.mockReset().mockResolvedValue(AsyncResult.success(undefined));
  state.search.mockReset().mockResolvedValue(AsyncResult.success({ entries: [] }));
  state.choose.mockReset().mockResolvedValue(null);
  state.openFile.mockReset();
  state.openExternal.mockReset().mockResolvedValue(undefined);
  state.openRemote.mockReset().mockResolvedValue(true);
  state.setMaximized.mockReset();
  state.remote = { mode: "local-exec" };
  state.copy.mockReset().mockResolvedValue(undefined);
  const { openFile, setMaximized } = useRightPanelStore.getState();
  useRightPanelStore.setState({ openFile: state.openFile, setMaximized: state.setMaximized });
  stack.defer(() => useRightPanelStore.setState({ openFile, setMaximized }));
  stack.use(vi.spyOn(navigator.clipboard, "writeText").mockImplementation(state.copy));
  stack.use(
    vi.spyOn(navigator.clipboard, "write").mockImplementation(async (items) => {
      const item = items[0]!;
      await state.copy(await (await item.getType("text/plain")).text());
    }),
  );
  return stack.move();
}

async function renderMarkdown(text: string) {
  const screen = await render(<ChatMarkdown cwd="/work" threadRef={threadRef} text={text} />);
  const anchor = () => screen.container.querySelector("a")!;
  return Object.assign(screen, {
    anchor,
    openContextMenu: () =>
      act(async () => {
        anchor().dispatchEvent(
          new MouseEvent("contextmenu", {
            bubbles: true,
            cancelable: true,
            clientX: 10,
            clientY: 10,
          }),
        );
      }),
  });
}

function offeredActions() {
  return state.choose.mock.lastCall![0].map((item: { id: string }) => item.id);
}

async function setAllowed(allowed: boolean) {
  await act(async () => {
    state.allowed = allowed;
    for (const listener of state.listeners) listener();
  });
}

it("removes host actions after revocation while keeping file preview and copying", async () => {
  using _setup = setup();
  await using screen = await renderMarkdown("[Readme](docs/readme.md)");
  await screen.openContextMenu();
  const fileActions = ["open-in-panel", "open-full-size"];
  const copies = ["download", "copy-relative", "copy-full"];
  expect(offeredActions()).toEqual([...fileActions, "open", "reveal", ...copies]);
  await setAllowed(false);
  state.choose.mockResolvedValue("copy-full");
  await screen.openContextMenu();
  expect(offeredActions()).toEqual([...fileActions, ...copies]);
  await vi.waitFor(() => expect(state.copy).toHaveBeenCalledWith("/work/docs/readme.md"));
  await act(async () => screen.anchor().click());
  expect(state.openFile).toHaveBeenCalledWith(threadRef, "docs/readme.md", undefined);
  expect(state.openEditor).not.toHaveBeenCalled();
});

it("does not launch an editor after revocation during a native context menu", async () => {
  using _setup = setup();
  let choose: (action: string) => void = () => {
    throw new Error("Menu not opened");
  };
  state.choose.mockImplementationOnce(
    () =>
      new Promise<string>((resolve) => {
        choose = resolve;
      }),
  );
  await using screen = await renderMarkdown("[Readme](docs/readme.md)");
  await screen.openContextMenu();
  expect(offeredActions()).toContain("open");
  await act(async () => {
    state.allowed = false;
    choose("open");
  });
  expect(state.openEditor).not.toHaveBeenCalled();
});

it("does not reveal a file after revocation during its workspace lookup", async () => {
  using _setup = setup();
  let finishLookup: () => void = () => {
    throw new Error("Lookup not started");
  };
  state.search.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finishLookup = () => resolve(AsyncResult.success({ entries: [] }));
      }),
  );
  state.choose.mockResolvedValue("reveal");
  await using screen = await renderMarkdown("[Readme](readme.md)");
  await screen.openContextMenu();
  expect(state.search).toHaveBeenCalledOnce();
  await act(async () => {
    state.allowed = false;
    finishLookup();
  });
  expect(state.openEditor).not.toHaveBeenCalled();
});

it.each(["link-to-thread", "unlink-from-thread"])(
  "rechecks %s after a native context menu returns",
  async (action) => {
    using _setup = setup();
    state.pullRequests = action === "unlink-from-thread" ? [linkedPullRequest] : [];
    const command = action === "link-to-thread" ? state.link : state.unlink;
    let choose: (action: string) => void = () => {
      throw new Error("Menu not opened");
    };
    state.choose.mockImplementationOnce(
      () =>
        new Promise<string>((resolve) => {
          choose = resolve;
        }),
    );
    await using screen = await renderMarkdown(`[Review](${linkedPullRequest.url})`);
    await screen.openContextMenu();
    expect(offeredActions()).toContain(action);
    await act(async () => {
      state.allowed = false;
      choose(action);
    });
    expect(command).not.toHaveBeenCalled();

    await setAllowed(true);
    state.choose.mockResolvedValue(action);
    await screen.openContextMenu();
    await vi.waitFor(() =>
      expect(command).toHaveBeenCalledExactlyOnceWith({
        environmentId: threadRef.environmentId,
        input: expect.objectContaining({
          threadId: threadRef.threadId,
          repository: "example/repo",
          number: 42,
        }),
      }),
    );
  },
);

it("opens a remote environment's file in this machine's editor at the linked line", async () => {
  using _setup = setup();
  state.remote = { mode: "remote-links", host: { kind: "ssh-alias", host: "devbox" } };
  state.choose.mockResolvedValue("open");
  await using screen = await renderMarkdown("[Setup](</work/my scripts/setup.ps1#L12>)");
  await screen.openContextMenu();
  expect(state.choose.mock.lastCall![0]).toContainEqual({ id: "open", label: "Open in VS Code" });
  await vi.waitFor(() =>
    expect(state.openRemote).toHaveBeenCalledExactlyOnceWith(
      "vscode://vscode-remote/ssh-remote+devbox/work/my%20scripts/setup.ps1:12:1",
    ),
  );
  expect(state.openEditor).not.toHaveBeenCalled();

  await act(async () => screen.anchor().click());
  expect(state.openFile).toHaveBeenCalledWith(threadRef, "my scripts/setup.ps1", 12);
});

it("opens a file full size and downloads it from the environment", async () => {
  using _setup = setup();
  state.choose.mockResolvedValueOnce("open-full-size").mockResolvedValueOnce("download");
  await using screen = await renderMarkdown("[Readme](/work/docs/readme.md:3)");
  await screen.openContextMenu();
  expect(state.openFile).toHaveBeenCalledWith(threadRef, "docs/readme.md", 3);
  expect(state.setMaximized).toHaveBeenCalledWith(threadRef, true);

  await screen.openContextMenu();
  await vi.waitFor(() =>
    expect(state.search).toHaveBeenCalledWith({
      environmentId: threadRef.environmentId,
      input: {
        resource: {
          _tag: "media-file",
          threadId: threadRef.threadId,
          path: "/work/docs/readme.md",
          disposition: "attachment",
        },
      },
    }),
  );
});

it("opens the file menu on a touch long press instead of following the link", async () => {
  using _setup = setup();
  await using screen = await renderMarkdown("[Readme](/work/docs/readme.md)");
  using _timers = fakeTimers();
  const press = (type: string) =>
    screen
      .anchor()
      .dispatchEvent(
        new PointerEvent(type, { bubbles: true, pointerType: "touch", clientX: 5, clientY: 5 }),
      );
  await act(async () => {
    press("pointerdown");
    vi.advanceTimersByTime(500);
    press("pointerup");
  });
  expect(state.choose).toHaveBeenCalledOnce();
  await screen.openContextMenu();
  await act(async () => screen.anchor().click());
  expect(state.choose).toHaveBeenCalledOnce();
  expect(state.openFile).not.toHaveBeenCalled();
});
