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
import { userEvent } from "vite-plus/test/browser";

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
  connection: null as { httpBaseUrl: string } | null,
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
  const actual = await original();
  return {
    ...actual,
    readPreparedConnection: ((id: EnvironmentId) =>
      state.connection ?? actual.readPreparedConnection(id)) as never,
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
import { MarkdownFileLink } from "./chat/MarkdownFileLink";

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
  state.connection = null;
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

it.each(["[Docs](https://example.com/docs)", "[Readme](docs/readme.md)"])(
  "tells the native menu for %s whether the keyboard opened it",
  async (text) => {
    using _setup = setup();
    await using screen = await renderMarkdown(text);
    screen.anchor().focus();
    await userEvent.keyboard("{Shift>}{F10}{/Shift}");
    await vi.waitFor(() => expect(state.choose.mock.lastCall?.[2]).toBe("keyboard"));
    await userEvent.click(screen.anchor(), { button: "right" });
    await vi.waitFor(() => expect(state.choose.mock.lastCall?.[2]).toBe("mouse"));
  },
);

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

it("preserves keyboard activation of a file-options button", async () => {
  using _setup = setup();
  await using screen = await render(
    <MarkdownFileLink
      environmentId={null}
      href="readme.md"
      targetPath="readme.md"
      iconPath="readme.md"
      workspaceRelativePath="readme.md"
      panelPath={null}
      label="Readme"
      copyMarkdown="[Readme](readme.md)"
      theme="dark"
      onOpenInPanel={vi.fn()}
      resolveCopyPaths={async () => ({
        relativePath: "readme.md",
        fullPath: "/work/readme.md",
        pathOnly: "/work/readme.md",
      })}
      openInEditorMenuLabel="Open in editor"
    />,
  );
  const button = screen.container.querySelector("button")!;
  button.focus();
  for (const key of ["{Enter}", " "]) {
    await userEvent.keyboard(key);
    await vi.waitFor(() => expect(state.choose.mock.lastCall?.[2]).toBe("keyboard"));
    state.choose.mockClear();
  }
  await userEvent.click(button);
  await vi.waitFor(() => expect(state.choose.mock.lastCall?.[2]).toBe("mouse"));
});

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
  using disposables = new DisposableStack();
  const stack = (disposable: Disposable) => disposables.use(disposable);
  state.choose.mockResolvedValueOnce("open-full-size").mockResolvedValueOnce("download");
  await using screen = await renderMarkdown("[Readme](/work/docs/readme.md:3)");
  await screen.openContextMenu();
  expect(state.openFile).toHaveBeenCalledWith(threadRef, "docs/readme.md", 3);
  expect(state.setMaximized).toHaveBeenCalledWith(threadRef, true);

  state.connection = { httpBaseUrl: "http://environment.test/" };
  state.search.mockResolvedValue(AsyncResult.success({ relativeUrl: "/api/assets/readme?t=1" }));
  const fetched: string[] = [];
  stack(
    vi.spyOn(window, "fetch").mockImplementation(async (input) => {
      fetched.push(String(input));
      return new Response("# Readme");
    }),
  );
  const saved: { name: string; text: Promise<string> }[] = [];
  const click = HTMLAnchorElement.prototype.click;
  stack(
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      if (!this.download) return click.call(this);
      saved.push({ name: this.download, text: fetch(this.href).then((r) => r.text()) });
    }),
  );
  await screen.openContextMenu();
  await vi.waitFor(() => expect(saved).toHaveLength(1));
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
  });
  expect(fetched[0]).toBe("http://environment.test/api/assets/readme?t=1");
  expect(saved[0]!.name).toBe("readme.md");
  expect(await saved[0]!.text).toBe("# Readme");
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
  expect(state.choose.mock.lastCall?.[2]).toBe("mouse");
  await screen.openContextMenu();
  // The click that ends a touch press has detail 1; element.click() is keyboard-like (detail 0).
  await act(async () =>
    screen
      .anchor()
      .dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 1 })),
  );
  expect(state.choose).toHaveBeenCalledOnce();
  expect(state.openFile).not.toHaveBeenCalled();
});

function touch(target: Element, type: string, point = 5) {
  target.dispatchEvent(
    new PointerEvent(type, {
      bubbles: true,
      cancelable: true,
      pointerType: "touch",
      clientX: point,
      clientY: point,
    }),
  );
}

it.each([
  { platform: "Android (contextmenu while held)", liftFirst: false },
  { platform: "Windows touch (contextmenu on lift)", liftFirst: true },
])(
  "keeps the real long-press menu open when $platform sends its own contextmenu late",
  async ({ liftFirst }) => {
    using _setup = setup();
    const { dismissContextMenu, showContextMenuFallback } = await import("../contextMenuFallback");
    let shown: Promise<unknown> = Promise.resolve(undefined);
    state.choose.mockImplementation((items, position) => {
      shown = showContextMenuFallback(items, position);
      return shown;
    });
    await using screen = await renderMarkdown("[Readme](/work/docs/readme.md)");
    // The full set, so the menu's own animation frames (which arm its outside-click dismissal)
    // advance with the clock.
    using _timers = fakeTimers();
    const menu = () => document.querySelector('[data-context-menu="true"]');
    try {
      await act(async () => {
        touch(screen.anchor(), "pointerdown");
        vi.advanceTimersByTime(500);
      });
      expect(menu()).not.toBeNull();
      await act(async () => {
        // The platform's touch & hold delay runs past the menu's arming frames.
        vi.advanceTimersByTime(500);
        if (liftFirst) touch(screen.anchor(), "pointerup");
        screen.anchor().dispatchEvent(
          new MouseEvent("contextmenu", {
            bubbles: true,
            cancelable: true,
            clientX: 5,
            clientY: 5,
          }),
        );
        if (!liftFirst) touch(screen.anchor(), "pointerup");
        vi.advanceTimersByTime(100);
      });
      expect(menu()).not.toBeNull();
      expect(state.choose).toHaveBeenCalledOnce();
      // The next real gesture still dismisses the menu.
      await act(async () => {
        document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
        vi.advanceTimersByTime(100);
      });
      expect(menu()).toBeNull();
      expect(await shown).toBeNull();
    } finally {
      dismissContextMenu();
    }
  },
);

it.each([
  { press: "ends with a click", endingClick: true },
  // Phones usually send no click after a long press that opened a menu.
  { press: "ends with no click", endingClick: false },
])(
  "takes keyboard Enter and the menu key on a chip after a long press that $press",
  async ({ endingClick }) => {
    using _setup = setup();
    await using screen = await renderMarkdown("[Readme](/work/docs/readme.md)");
    {
      using _timers = fakeTimers();
      await act(async () => {
        touch(screen.anchor(), "pointerdown");
        vi.advanceTimersByTime(500);
        touch(screen.anchor(), "pointerup");
        // The click that ends the touch press is not an open.
        if (endingClick) {
          screen
            .anchor()
            .dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 1 }));
        }
      });
    }
    expect(state.choose).toHaveBeenCalledOnce();
    expect(state.openFile).not.toHaveBeenCalled();

    await act(async () => {
      screen.anchor().focus();
      screen.anchor().dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Enter" }));
      screen
        .anchor()
        .dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: 0 }));
    });
    expect(state.openFile).toHaveBeenCalledWith(threadRef, "docs/readme.md", undefined);

    await act(async () => {
      screen
        .anchor()
        .dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "ContextMenu" }));
      screen
        .anchor()
        .dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    });
    expect(state.choose).toHaveBeenCalledTimes(2);
  },
);

it("takes a right-click and a new long press after a long press that got no late contextmenu", async () => {
  // iOS sends no contextmenu for a long press, so nothing ends the wait for one but the next
  // gesture.
  using _setup = setup();
  await using screen = await renderMarkdown("[Readme](/work/docs/readme.md)");
  using _timers = fakeTimers();
  const longPress = async () =>
    act(async () => {
      touch(screen.anchor(), "pointerdown");
      vi.advanceTimersByTime(500);
      touch(screen.anchor(), "pointerup");
    });
  const rightClick = async () =>
    act(async () => {
      screen.anchor().dispatchEvent(
        new PointerEvent("pointerdown", {
          bubbles: true,
          cancelable: true,
          pointerType: "mouse",
          button: 2,
          clientX: 5,
          clientY: 5,
        }),
      );
      screen.anchor().dispatchEvent(
        new MouseEvent("contextmenu", {
          bubbles: true,
          cancelable: true,
          clientX: 5,
          clientY: 5,
        }),
      );
    });
  await longPress();
  expect(state.choose).toHaveBeenCalledTimes(1);
  await rightClick();
  expect(state.choose).toHaveBeenCalledTimes(2);
  await longPress();
  expect(state.choose).toHaveBeenCalledTimes(3);
  await rightClick();
  expect(state.choose).toHaveBeenCalledTimes(4);
  expect(state.openFile).not.toHaveBeenCalled();
});

it("does not open a menu for a chip that unmounts during a long press", async () => {
  using _setup = setup();
  const screen = await renderMarkdown("[Readme](/work/docs/readme.md)");
  using _timers = fakeTimers();
  await act(async () => touch(screen.anchor(), "pointerdown"));
  await screen.unmount();
  vi.advanceTimersByTime(1_000);
  expect(state.choose).not.toHaveBeenCalled();
});
