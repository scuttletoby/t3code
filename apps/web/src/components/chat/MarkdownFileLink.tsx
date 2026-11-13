import { useAssetUrlState } from "../../assets/assetUrls";
import { formatFilePathForClipboard } from "@t3tools/client-runtime/media-actions";
import {
  isAtomCommandInterrupted,
  squashAtomCommandFailure,
  type AtomCommandResult,
} from "@t3tools/client-runtime/state/runtime";
import type { ContextMenuSource, EnvironmentId, ScopedThreadRef } from "@t3tools/contracts";
import {
  memo,
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import { contextMenuSourceOf } from "../../contextMenuSource";
import { readLocalApi } from "../../localApi";
import { cn } from "../../lib/utils";
import { shouldOpenMarkdownFileLinkInEditor } from "../../markdown-links";
import type { markdownFileCopyPaths } from "../../markdownFileHostPath";
import { documentChipKindForFileName } from "../../pierre-icons";
import { ContextChip } from "../ContextChip";
import { stackedThreadToast, toastManager } from "../ui/toast";
import { Tooltip, TooltipPopup, TooltipTrigger } from "../ui/tooltip";
import { FileTagChipContent } from "./FileTagChip";
import {
  hasMarkdownFilePrimaryAction,
  markdownFileMenuItems,
  shouldUseMarkdownFileBrowserPrimaryAction,
} from "./MarkdownFileLink.logic";
import { reportMarkdownActionFailure } from "./markdownActionFailure";
import { WorkspaceEntryIcon, WorkspaceEntryTooltip } from "./WorkspaceEntryIcon";

interface MarkdownFileLinkProps {
  environmentId: EnvironmentId | null;
  href: string;
  targetPath: string;
  iconPath: string;
  isAppBundle?: boolean;
  workspaceRelativePath: string | null;
  panelPath: string | null;
  line?: number | undefined;
  label: string;
  copyMarkdown: string;
  theme: "light" | "dark";
  threadRef?: ScopedThreadRef | undefined;
  onOpen?: (() => Promise<AtomCommandResult<unknown, unknown>>) | undefined;
  onOpenInPanel: (panelPath: string, line: number | undefined) => void;
  onOpenFullSize?: (() => void) | undefined;
  onDownload?: (() => Promise<void>) | undefined;
  resolveCopyPaths: () => Promise<ReturnType<typeof markdownFileCopyPaths>>;
  openInEditorMenuLabel: string;
  onOpenInBrowser?: (() => Promise<AtomCommandResult<unknown, unknown>>) | undefined;
  onOpenMedia?: (() => void) | undefined;
  onReveal?: (() => Promise<AtomCommandResult<unknown, unknown>>) | undefined;
  revealLabel?: string | undefined;
}

const MARKDOWN_FILE_LINK_CLASS_NAME = "chat-markdown-file-link";
const LONG_PRESS_MS = 500;
const LONG_PRESS_SLOP_PX = 10;

export const MarkdownFileLink = memo(function MarkdownFileLink({
  environmentId,
  href,
  targetPath,
  iconPath,
  isAppBundle = false,
  workspaceRelativePath,
  panelPath,
  line,
  label,
  copyMarkdown,
  theme,
  threadRef,
  onOpen,
  onOpenInPanel,
  onOpenFullSize,
  onDownload,
  openInEditorMenuLabel,
  onOpenInBrowser,
  onOpenMedia,
  onReveal,
  revealLabel,
  resolveCopyPaths,
}: MarkdownFileLinkProps) {
  const handleOpenInEditor = () => {
    if (!onOpen) {
      return;
    }
    void (async () => {
      try {
        const result = await onOpen();
        if (result._tag === "Success" || isAtomCommandInterrupted(result)) {
          return;
        }
        reportMarkdownActionFailure(
          { operation: "open-file-in-editor", target: targetPath },
          result.cause,
        );
        const error = squashAtomCommandFailure(result);
        toastManager.add(
          stackedThreadToast({
            type: "error",
            title: "Unable to open file",
            description: error instanceof Error ? error.message : "An error occurred.",
          }),
        );
      } catch (cause) {
        reportMarkdownActionFailure(
          { operation: "open-file-in-editor", target: targetPath },
          cause,
        );
        toastManager.add(
          stackedThreadToast({
            type: "error",
            title: "Unable to open file",
            description: cause instanceof Error ? cause.message : "An error occurred.",
          }),
        );
      }
    })();
  };

  const handleOpenInFilePreview = () => {
    if (threadRef && panelPath) {
      onOpenInPanel(panelPath, line);
      return;
    }
    if (onOpenMedia) {
      onOpenMedia();
      return;
    }
    handleOpenInEditor();
  };

  const handleOpenInBrowser = () => {
    if (!onOpenInBrowser) {
      return;
    }
    void (async () => {
      try {
        const result = await onOpenInBrowser();
        if (result._tag === "Success" || isAtomCommandInterrupted(result)) {
          return;
        }
        reportMarkdownActionFailure(
          { operation: "open-file-in-browser", target: targetPath },
          result.cause,
        );
        const error = squashAtomCommandFailure(result);
        toastManager.add(
          stackedThreadToast({
            type: "error",
            title: "Unable to open file in browser",
            description: error instanceof Error ? error.message : "An error occurred.",
          }),
        );
      } catch (cause) {
        reportMarkdownActionFailure(
          { operation: "open-file-in-browser", target: targetPath },
          cause,
        );
        toastManager.add(
          stackedThreadToast({
            type: "error",
            title: "Unable to open file in browser",
            description: cause instanceof Error ? cause.message : "An error occurred.",
          }),
        );
      }
    })();
  };

  const handleRevealInFileManager = () => {
    if (!onReveal) {
      return;
    }
    void (async () => {
      try {
        const result = await onReveal();
        if (result._tag === "Success" || isAtomCommandInterrupted(result)) {
          return;
        }
        reportMarkdownActionFailure(
          { operation: "reveal-file-in-file-manager", target: targetPath },
          result.cause,
        );
        const error = squashAtomCommandFailure(result);
        toastManager.add(
          stackedThreadToast({
            type: "error",
            title: "Unable to reveal file",
            description: error instanceof Error ? error.message : "An error occurred.",
          }),
        );
      } catch (cause) {
        reportMarkdownActionFailure(
          { operation: "reveal-file-in-file-manager", target: targetPath },
          cause,
        );
        toastManager.add(
          stackedThreadToast({
            type: "error",
            title: "Unable to reveal file",
            description: cause instanceof Error ? cause.message : "An error occurred.",
          }),
        );
      }
    })();
  };

  const handleDownload = () => {
    if (!onDownload) return;
    onDownload().catch((cause: unknown) => {
      reportMarkdownActionFailure({ operation: "download-file", target: targetPath }, cause);
      toastManager.add(
        stackedThreadToast({
          type: "error",
          title: "Unable to download file",
          description: cause instanceof Error ? cause.message : "An error occurred.",
        }),
      );
    });
  };

  const handleCopy = (resolveValue: () => Promise<string>, title: string) => {
    if (typeof window === "undefined" || !navigator.clipboard?.writeText) {
      toastManager.add(
        stackedThreadToast({
          type: "error",
          title: `Failed to copy ${title.toLowerCase()}`,
          description: "Clipboard API unavailable.",
        }),
      );
      return;
    }

    const value = resolveValue().then((text) => formatFilePathForClipboard(text, targetPath));
    // Start the write before workspace lookup finishes so Safari retains the user gesture.
    const write =
      typeof navigator.clipboard.write === "function" && typeof ClipboardItem !== "undefined"
        ? navigator.clipboard.write([
            new ClipboardItem({
              "text/plain": value.then((text) => new Blob([text], { type: "text/plain" })),
            }),
          ])
        : value.then((text) => navigator.clipboard.writeText(text));
    void Promise.all([write, value]).catch((error) => {
      reportMarkdownActionFailure(
        { operation: "copy-file-path", target: targetPath, copyTarget: title },
        error,
      );
      toastManager.add(
        stackedThreadToast({
          type: "error",
          title: `Failed to copy ${title.toLowerCase()}`,
          description: error instanceof Error ? error.message : "An error occurred.",
        }),
      );
    });
  };

  const showFileContextMenu = async (
    position: { x: number; y: number },
    source: ContextMenuSource,
  ) => {
    const api = readLocalApi();
    if (!api) return;

    try {
      const clicked = await api.contextMenu.show(
        markdownFileMenuItems({
          hasLine: line !== undefined,
          workspaceRelativePath,
          canOpenMedia: onOpenMedia !== undefined,
          canOpenInPanel,
          openInEditorLabel: onOpen ? openInEditorMenuLabel : null,
          canOpenInBrowser: onOpenInBrowser !== undefined,
          revealLabel: onReveal && revealLabel ? revealLabel : null,
          canDownload: onDownload !== undefined,
        }),
        position,
        source,
      );

      if (clicked === "preview-media") {
        onOpenMedia?.();
      } else if (clicked === "open-in-panel" && panelPath) {
        onOpenInPanel(panelPath, line);
      } else if (clicked === "open-full-size") {
        onOpenFullSize?.();
      } else if (clicked === "open") {
        handleOpenInEditor();
      } else if (clicked === "open-in-browser") {
        handleOpenInBrowser();
      } else if (clicked === "reveal") {
        handleRevealInFileManager();
      } else if (clicked === "download") {
        handleDownload();
      } else if (
        clicked === "copy-relative" ||
        clicked === "copy-full" ||
        clicked === "copy-path-only"
      ) {
        const copy = {
          "copy-relative": ["relativePath", "Relative path"],
          "copy-full": ["fullPath", "Full path"],
          "copy-path-only": ["pathOnly", "Full path"],
        } as const;
        const [key, title] = copy[clicked];
        handleCopy(() => resolveCopyPaths().then((paths) => paths[key]), title);
      }
    } catch (cause) {
      reportMarkdownActionFailure(
        { operation: "show-file-context-menu", target: targetPath },
        cause,
      );
    }
  };

  // iOS Safari never fires contextmenu for a touch, so a long press opens the same menu.
  const longPress = useRef<{
    timer: ReturnType<typeof setTimeout> | undefined;
    x: number;
    y: number;
    fired: boolean;
    releaseContextMenu: (() => void) | undefined;
  }>({ timer: undefined, x: 0, y: 0, fired: false, releaseContextMenu: undefined });
  const cancelLongPress = () => {
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
  const handlePointerDown = (event: ReactPointerEvent<HTMLElement>) => {
    cancelLongPress();
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
      void showFileContextMenu({ x, y }, "mouse");
    }, LONG_PRESS_MS);
  };
  // Only the click that ends the touch press; keyboard activation has detail 0.
  const consumeLongPressClick = (event: ReactMouseEvent<HTMLElement>) => {
    if (!longPress.current.fired || event.detail === 0) return false;
    longPress.current.fired = false;
    return true;
  };
  const handlePointerMove = (event: ReactPointerEvent<HTMLElement>) => {
    const { x, y } = longPress.current;
    if (Math.hypot(event.clientX - x, event.clientY - y) > LONG_PRESS_SLOP_PX) cancelLongPress();
  };
  const longPressHandlers = {
    onPointerDown: handlePointerDown,
    onPointerMove: handlePointerMove,
    onPointerUp: cancelLongPress,
    onPointerCancel: cancelLongPress,
  };

  const handleContextMenu = (event: ReactMouseEvent<HTMLElement>) => {
    event.preventDefault();
    event.stopPropagation();
    cancelLongPress();
    const position =
      event.clientX === 0 && event.clientY === 0
        ? (() => {
            const bounds = event.currentTarget.getBoundingClientRect();
            return { x: bounds.left, y: bounds.bottom };
          })()
        : { x: event.clientX, y: event.clientY };
    void showFileContextMenu(position, contextMenuSourceOf(event));
  };

  const canOpenInEditor = onOpen !== undefined;
  const canOpenInBrowser = onOpenInBrowser !== undefined;
  const canOpenInPanel = threadRef !== undefined && Boolean(panelPath);
  const hasPrimaryAction = hasMarkdownFilePrimaryAction({
    canOpenInEditor,
    canOpenInBrowser,
    canOpenInPanel,
    canOpenMedia: onOpenMedia !== undefined,
  });
  const useBrowserPrimaryAction = shouldUseMarkdownFileBrowserPrimaryAction({
    iconPath,
    canOpenInEditor,
    canOpenInBrowser,
    canOpenInPanel,
  });
  const chipKind = documentChipKindForFileName(iconPath) ?? "mention";

  return (
    <Tooltip>
      <TooltipTrigger
        render={
          hasPrimaryAction ? (
            <ContextChip
              kind={chipKind}
              dir="ltr"
              render={<a href={href} />}
              className={MARKDOWN_FILE_LINK_CLASS_NAME}
              data-markdown-copy={copyMarkdown}
              {...longPressHandlers}
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                if (consumeLongPressClick(event)) return;
                if (onOpen && shouldOpenMarkdownFileLinkInEditor(event)) {
                  handleOpenInEditor();
                  return;
                }
                if (useBrowserPrimaryAction) {
                  handleOpenInBrowser();
                  return;
                }
                handleOpenInFilePreview();
              }}
              onContextMenu={handleContextMenu}
            >
              <FileTagChipContent
                icon={
                  isAppBundle ? (
                    <AppBundleIcon environmentId={environmentId} path={iconPath} theme={theme} />
                  ) : undefined
                }
                path={iconPath}
                environmentId={environmentId}
                label={label}
                theme={theme}
              />
            </ContextChip>
          ) : (
            <ContextChip
              kind={chipKind}
              dir="ltr"
              render={<button type="button" />}
              aria-label={`File options for ${label}`}
              aria-haspopup="menu"
              className={cn(MARKDOWN_FILE_LINK_CLASS_NAME, "select-text")}
              data-markdown-copy={copyMarkdown}
              {...longPressHandlers}
              onClick={(event) => {
                if (!consumeLongPressClick(event)) {
                  handleContextMenu(event);
                  return;
                }
                event.preventDefault();
                event.stopPropagation();
              }}
              onContextMenu={handleContextMenu}
            >
              <FileTagChipContent
                icon={
                  isAppBundle ? (
                    <AppBundleIcon environmentId={environmentId} path={iconPath} theme={theme} />
                  ) : undefined
                }
                path={iconPath}
                environmentId={environmentId}
                label={label}
                theme={theme}
              />
            </ContextChip>
          )
        }
      />
      <TooltipPopup side="top" variant="code">
        <WorkspaceEntryTooltip path={iconPath} label={targetPath} environmentId={environmentId} />
      </TooltipPopup>
    </Tooltip>
  );
}, areMarkdownFileLinkPropsEqual);

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

function areMarkdownFileLinkPropsEqual(
  previous: Readonly<MarkdownFileLinkProps>,
  next: Readonly<MarkdownFileLinkProps>,
): boolean {
  return (
    previous.href === next.href &&
    previous.targetPath === next.targetPath &&
    previous.iconPath === next.iconPath &&
    previous.environmentId === next.environmentId &&
    previous.workspaceRelativePath === next.workspaceRelativePath &&
    previous.panelPath === next.panelPath &&
    previous.line === next.line &&
    previous.label === next.label &&
    previous.copyMarkdown === next.copyMarkdown &&
    previous.theme === next.theme &&
    previous.threadRef === next.threadRef &&
    previous.isAppBundle === next.isAppBundle &&
    previous.onOpen === next.onOpen &&
    previous.onOpenInPanel === next.onOpenInPanel &&
    previous.onOpenFullSize === next.onOpenFullSize &&
    previous.onDownload === next.onDownload &&
    previous.openInEditorMenuLabel === next.openInEditorMenuLabel &&
    previous.onOpenInBrowser === next.onOpenInBrowser &&
    previous.onOpenMedia === next.onOpenMedia &&
    previous.onReveal === next.onReveal &&
    previous.resolveCopyPaths === next.resolveCopyPaths &&
    previous.revealLabel === next.revealLabel
  );
}

function AppBundleIcon(props: {
  environmentId: EnvironmentId | null;
  path: string;
  theme: "light" | "dark";
}) {
  const asset = useAssetUrlState(props.environmentId, {
    _tag: "native-app-icon",
    app: { _tag: "app-path", path: props.path },
  });
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  if (asset._tag !== "Success" || asset.url === failedUrl) {
    return <WorkspaceEntryIcon {...props} />;
  }
  return (
    <img
      src={asset.url}
      alt=""
      className="size-4 shrink-0 object-contain"
      onError={() => setFailedUrl(asset.url)}
    />
  );
}
