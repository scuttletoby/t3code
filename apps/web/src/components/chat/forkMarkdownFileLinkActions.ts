// Fork: what a chat file link does beyond upstream's (#17): a long press opens its menu on touch
// devices, a file on another machine opens in the viewing device's editor over SSH, and a link
// naming an environment downloads from it.
import { useAtomValue } from "@effect/atom-react";
import {
  AuthFilesystemReadScope,
  type ContextMenuSource,
  type EnvironmentId,
} from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import { AsyncResult } from "effect/reactivity";
import {
  useEffect,
  useRef,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { openInEditorMenuLabel } from "../../editorLabels";
import { usePreferredEditor } from "../../editorPreferences";
import type { MessageResolver } from "../../i18n/messages";
import type { MarkdownFileLinkMeta } from "../../markdown-links";
import {
  openRemoteEditorUrl,
  useRemoteCapableEditors,
  type RemoteOpenState,
} from "../../remoteOpen";
import { appAtomRegistry } from "../../rpc/atomRegistry";
import { assetEnvironment } from "../../state/assets";
import { readEnvironment } from "../../state/environments";
import { environmentPresentations } from "../../state/presentation";
import {
  environmentSession,
  readEnvironmentScope,
  readPreparedConnection,
} from "../../state/session";
import { useAtomQueryRunner } from "../../state/use-atom-query-runner";
import { downloadFile } from "../media/mediaContent";
import {
  environmentFileLinkProps,
  type EnvironmentFileAccessReaders,
} from "./forkEnvironmentFileLink";
import { markdownFileRemoteEditorUrl } from "./forkMarkdownFileLinkMenu";

const LONG_PRESS_MS = 500;
const LONG_PRESS_SLOP_PX = 10;

/** iOS Safari never fires contextmenu for a touch, so a long press opens the same menu. */
export function useForkMarkdownFileLongPress(
  showMenu: (position: { x: number; y: number }, source: ContextMenuSource) => unknown,
) {
  const longPress = useRef<{
    timer: ReturnType<typeof setTimeout> | undefined;
    x: number;
    y: number;
    fired: boolean;
    releaseContextMenu: (() => void) | undefined;
  }>({ timer: undefined, x: 0, y: 0, fired: false, releaseContextMenu: undefined });
  const cancel = () => {
    clearTimeout(longPress.current.timer);
    longPress.current.timer = undefined;
  };
  const releaseLateContextMenu = () => {
    longPress.current.releaseContextMenu?.();
    longPress.current.releaseContextMenu = undefined;
  };
  // oxlint-disable-next-line t3code/no-unreasoned-state-sync -- clears the long-press timer and the window contextmenu listener this chip holds until unmount.
  useEffect(() => {
    const pending = longPress.current;
    return () => {
      clearTimeout(pending.timer);
      pending.releaseContextMenu?.();
    };
  }, []);
  const onPointerDown = (event: ReactPointerEvent<HTMLElement>) => {
    cancel();
    releaseLateContextMenu();
    longPress.current.fired = false;
    if (event.pointerType !== "touch") return;
    const chip = event.currentTarget;
    const { clientX: x, clientY: y } = event;
    longPress.current.x = x;
    longPress.current.y = y;
    longPress.current.timer = setTimeout(() => {
      longPress.current.timer = undefined;
      longPress.current.fired = true;
      // Android (after its touch & hold delay) and Windows touch (on lift) send their own
      // contextmenu for this press after the timer. The open menu would take it as an outside
      // right-click and close, so the first one on the chip is swallowed before anything sees it.
      longPress.current.releaseContextMenu = swallowLateContextMenu(chip);
      // A touch press is a pointer gesture: the native menu must not preselect its first item.
      void showMenu({ x, y }, "mouse");
    }, LONG_PRESS_MS);
  };
  const onPointerMove = (event: ReactPointerEvent<HTMLElement>) => {
    const { x, y } = longPress.current;
    if (Math.hypot(event.clientX - x, event.clientY - y) > LONG_PRESS_SLOP_PX) cancel();
  };
  // Only the click that ends the touch press; keyboard activation has detail 0.
  const consumeClick = (event: ReactMouseEvent<HTMLElement>) => {
    if (!longPress.current.fired || event.detail === 0) return false;
    longPress.current.fired = false;
    return true;
  };
  return {
    handlers: { onPointerDown, onPointerMove, onPointerUp: cancel, onPointerCancel: cancel },
    consumeClick,
    /**
     * For the handler that opens the menu from a click or a contextmenu event: drops a pending
     * long press, and says whether this is the click ending a long press that already opened it.
     */
    opensNoMenu: (event: ReactMouseEvent<HTMLElement>) => {
      cancel();
      return event.type === "click" && consumeClick(event);
    },
  };
}

// Window capture runs before the menu's own document listeners. The next pointer or key press is
// a new gesture, so it ends the wait (iOS sends no contextmenu at all).
function swallowLateContextMenu(chip: HTMLElement): () => void {
  const onContextMenu = (event: MouseEvent) => {
    if (!(event.target instanceof Node) || !chip.contains(event.target)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    release();
  };
  const release = () => {
    window.removeEventListener("contextmenu", onContextMenu, true);
    window.removeEventListener("pointerdown", release, true);
    window.removeEventListener("keydown", release, true);
  };
  window.addEventListener("contextmenu", onContextMenu, true);
  window.addEventListener("pointerdown", release, true);
  window.addEventListener("keydown", release, true);
  return release;
}

const environmentFileReaders: Omit<EnvironmentFileAccessReaders, "label"> = {
  httpBaseUrl: (id) => readPreparedConnection(id)?.httpBaseUrl ?? null,
  canReadFiles: (id) => readEnvironmentScope(id, AuthFilesystemReadScope),
  reconnecting: (id) => {
    const phase = readEnvironment(id)?.connection.phase;
    if (phase === "connecting" || phase === "reconnecting") return true;
    // Connected, but its session (and so its scopes) is not read yet.
    const session = appAtomRegistry.get(environmentSession.sessionStateAtom(id));
    return phase === "connected" && (AsyncResult.isInitial(session) || session.waiting);
  },
};

/** Saved environment names as one string, so the chat re-renders only when a name changes. */
function environmentNamesKey(
  presentations: ReadonlyMap<
    EnvironmentId,
    { readonly entry: { readonly target: { readonly label: string } } }
  >,
): string {
  return JSON.stringify([...presentations].map(([id, { entry }]) => [id, entry.target.label]));
}

/**
 * The props a file link takes after upstream's own: a link naming an environment downloads from
 * it, and over SSH the viewing device's editor opens the file.
 */
export function useForkMarkdownFileRemoteEditor(
  remoteOpenState: RemoteOpenState,
  cwd: string | undefined,
  messages: MessageResolver,
) {
  const remoteCapableEditors = useRemoteCapableEditors();
  const [editor] = usePreferredEditor(remoteCapableEditors);
  const createAssetUrl = useAtomQueryRunner(assetEnvironment.createUrl, {
    reportFailure: false,
    refresh: true,
  });
  const namesKey = useAtomValue(environmentPresentations.presentationsAtom, environmentNamesKey);
  const names = new Map<string, string>(JSON.parse(namesKey) as Array<[string, string]>);
  const readers: EnvironmentFileAccessReaders = {
    ...environmentFileReaders,
    label: (id) => names.get(id) ?? null,
  };
  const remoteEditor =
    remoteOpenState.mode === "remote-links" && editor !== null
      ? { editor, host: remoteOpenState.host }
      : null;
  const open = async (fileLinkMeta: MarkdownFileLinkMeta, path: string) => {
    const url =
      remoteEditor &&
      markdownFileRemoteEditorUrl({
        editor: remoteEditor.editor,
        host: remoteEditor.host,
        path,
        cwd,
        line: fileLinkMeta.line,
        column: fileLinkMeta.column,
      });
    if (url && (await openRemoteEditorUrl(url))) {
      return AsyncResult.success<void>(undefined);
    }
    return AsyncResult.failure<void, Error>(
      Cause.fail(new Error("The editor could not open this file over SSH.")),
    );
  };
  return {
    /**
     * Upstream's link opens an app bundle, or a file through the environment's own shell, itself;
     * otherwise over SSH the link opens the file in this editor, and the menu names this editor
     * unless the link is an app bundle. A link naming an environment replaces all of that.
     */
    linkProps: (input: {
      canOpenApp: boolean;
      canUseShellActions: boolean;
      fileLinkMeta: MarkdownFileLinkMeta;
      resolveAbsolutePath: () => Promise<string>;
    }) =>
      environmentFileLinkProps(input.fileLinkMeta, {
        readers,
        createAssetUrl,
        downloadFile,
      }) ??
      (remoteEditor === null
        ? {}
        : {
            ...(input.canOpenApp
              ? {}
              : { openInEditorMenuLabel: openInEditorMenuLabel(remoteEditor.editor, messages) }),
            ...(input.canOpenApp || input.canUseShellActions
              ? {}
              : {
                  onOpen: async () => open(input.fileLinkMeta, await input.resolveAbsolutePath()),
                }),
          }),
  };
}
