// Fork: the fork's entries in a chat file link's menu, and its remote editor links (#17).
// Upstream's menu stays where upstream builds it, in MarkdownFileLink.tsx; this file only adds
// to it, so upstream's changes to its own entries and labels arrive without a conflict.
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own menu labels are English only; upstream's translation catalogs hold no fork keys. */
import {
  buildRemoteOpenUrl,
  type ContextMenuItem,
  type ContextMenuSource,
  type EditorId,
} from "@t3tools/contracts";
import { resolvePathLinkTarget } from "@t3tools/shared/fileLinks";
import { isAbsolutePath } from "@t3tools/shared/path";

import type { RemoteOpenHost } from "../../remoteOpen";

export type ForkMarkdownFileMenuAction =
  | "open-in-panel"
  | "open-full-size"
  | "download"
  | "copy-path-only";

export interface ForkMarkdownFileMenuInput {
  hasLine: boolean;
  canOpenInPanel: boolean;
  canDownload: boolean;
}

/**
 * Upstream's menu entries with the fork's added: the files viewer entries after the media
 * preview, the download after the other ways to open, the path without its line after the other
 * copies, and a separator between the two groups. An entry upstream adds later keeps its place.
 */
export function forkMarkdownFileMenuItems<T extends string>(
  upstreamItems: readonly ContextMenuItem<T>[],
  input: ForkMarkdownFileMenuInput,
): ContextMenuItem<T | ForkMarkdownFileMenuAction>[] {
  type Item = ContextMenuItem<T | ForkMarkdownFileMenuAction>;
  const isCopy = (item: Item) => item.id.startsWith("copy-");
  const opens: Item[] = upstreamItems.filter((item) => !isCopy(item));
  const copies: Item[] = upstreamItems.filter(isCopy);
  if (input.canOpenInPanel) {
    const afterPreview = opens.findIndex((item) => item.id === "preview-media") + 1;
    opens.splice(
      afterPreview,
      0,
      { id: "open-in-panel", label: "Open in files viewer" },
      { id: "open-full-size", label: "Open full size" },
    );
  }
  if (input.canDownload) opens.push({ id: "download", label: "Download to this device" });
  if (input.hasLine) copies.push({ id: "copy-path-only", label: "Copy full path without line" });
  const [firstCopy, ...otherCopies] = copies;
  return [
    ...opens,
    ...(firstCopy ? [{ ...firstCopy, separatorBefore: opens.length > 0 }] : []),
    ...otherCopies,
  ];
}

/** The context menu upstream's file link opens, showing the fork's entries as well. */
export function forkMarkdownFileMenu(
  contextMenu: {
    show: <T extends string>(
      items: readonly ContextMenuItem<T>[],
      position?: { x: number; y: number },
      source?: ContextMenuSource,
    ) => Promise<T | null>;
  },
  input: ForkMarkdownFileMenuInput,
) {
  return {
    show: <T extends string>(
      items: readonly ContextMenuItem<T>[],
      position?: { x: number; y: number },
      source?: ContextMenuSource,
    ): Promise<T | ForkMarkdownFileMenuAction | null> =>
      contextMenu.show(forkMarkdownFileMenuItems(items, input), position, source),
  };
}

// The editor runs on the viewing machine and reaches the environment's files over SSH.
export function markdownFileRemoteEditorUrl(input: {
  editor: EditorId;
  host: RemoteOpenHost;
  path: string;
  cwd: string | undefined;
  line: number | undefined;
  column: number | undefined;
}): string | undefined {
  const absolutePath =
    isAbsolutePath(input.path) || input.cwd === undefined
      ? input.path
      : resolvePathLinkTarget(input.path, input.cwd);
  if (!isAbsolutePath(absolutePath)) return undefined;
  return buildRemoteOpenUrl({
    editor: input.editor,
    host: input.host.host,
    ...(input.host.username === undefined ? {} : { username: input.host.username }),
    absolutePath,
    isFile: true,
    ...(input.line === undefined ? {} : { line: input.line }),
    ...(input.column === undefined ? {} : { column: input.column }),
  });
}
