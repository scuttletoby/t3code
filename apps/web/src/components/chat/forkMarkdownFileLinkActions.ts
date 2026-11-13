// Fork: what a chat file link does beyond upstream's (#17): a long press opens its menu on touch
// devices, a file downloads to the viewing device, and a file on another machine opens in the
// viewing device's editor over SSH. MarkdownFileLink.tsx and ChatMarkdown.tsx only call these.
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own messages are English only; upstream's translation catalogs hold no fork keys. */
import type { ContextMenuSource } from "@t3tools/contracts";
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
import { stackedThreadToast, toastManager } from "../ui/toast";
import { markdownFileRemoteEditorUrl } from "./forkMarkdownFileLinkMenu";
import { reportMarkdownActionFailure } from "./markdownActionFailure";

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

/** Downloads the linked file; a failure is reported the way the link's other actions report one. */
export function forkDownloadMarkdownFile(
  onDownload: (() => Promise<void>) | undefined,
  targetPath: string,
  unknownErrorDescription: string,
): void {
  if (!onDownload) return;
  onDownload().catch((cause: unknown) => {
    reportMarkdownActionFailure({ operation: "download-file", target: targetPath }, cause);
    toastManager.add(
      stackedThreadToast({
        type: "error",
        title: "Unable to download file",
        description: cause instanceof Error ? cause.message : unknownErrorDescription,
      }),
    );
  });
}

/**
 * The editor on the viewing device that reaches the environment's files over SSH, or null when
 * links open another way. `linkProps` are the props a file link takes after upstream's own.
 */
export function useForkMarkdownFileRemoteEditor(
  remoteOpenState: RemoteOpenState,
  cwd: string | undefined,
  messages: MessageResolver,
) {
  const remoteCapableEditors = useRemoteCapableEditors();
  const [editor] = usePreferredEditor(remoteCapableEditors);
  if (remoteOpenState.mode !== "remote-links" || editor === null) return null;
  const { host } = remoteOpenState;
  const open = async (fileLinkMeta: MarkdownFileLinkMeta, path: string) => {
    const url = markdownFileRemoteEditorUrl({
      editor,
      host,
      path,
      cwd,
      line: fileLinkMeta.line,
      column: fileLinkMeta.column,
    });
    if (url !== undefined && (await openRemoteEditorUrl(url))) {
      return AsyncResult.success<void>(undefined);
    }
    return AsyncResult.failure<void, Error>(
      Cause.fail(new Error("The editor could not open this file over SSH.")),
    );
  };
  return {
    /**
     * Upstream's link opens an app bundle, or a file through the environment's own shell, itself;
     * otherwise the link opens the file in this editor. The menu names this editor unless the
     * link is an app bundle.
     */
    linkProps: (input: {
      canOpenApp: boolean;
      canUseShellActions: boolean;
      fileLinkMeta: MarkdownFileLinkMeta;
      resolveAbsolutePath: () => Promise<string>;
    }) => ({
      ...(input.canOpenApp
        ? {}
        : { openInEditorMenuLabel: openInEditorMenuLabel(editor, messages) }),
      ...(input.canOpenApp || input.canUseShellActions
        ? {}
        : { onOpen: async () => open(input.fileLinkMeta, await input.resolveAbsolutePath()) }),
    }),
  };
}
