import {
  buildRemoteOpenUrl,
  type ContextMenuItem,
  type EditorId,
  type EnvironmentId,
} from "@t3tools/contracts";
import { resolvePathLinkTarget } from "@t3tools/shared/fileLinks";
import { isAbsolutePath } from "@t3tools/shared/path";

import { shouldOpenMarkdownFileLinkInBrowserByDefault } from "../../markdown-links";
import type { RemoteOpenHost, RemoteOpenMode } from "../../remoteOpen";

export function canUseMarkdownFileShellActions(
  environmentId: EnvironmentId | null,
  remoteOpenMode: RemoteOpenMode,
  isRemoteOpenResolved: boolean,
): boolean {
  return environmentId !== null && isRemoteOpenResolved && remoteOpenMode === "local-exec";
}

export function hasMarkdownFilePrimaryAction(input: {
  canOpenInEditor: boolean;
  canOpenInBrowser: boolean;
  canOpenInPanel: boolean;
  canOpenMedia?: boolean;
}): boolean {
  return (
    input.canOpenInEditor ||
    input.canOpenInBrowser ||
    input.canOpenInPanel ||
    input.canOpenMedia === true
  );
}

export type MarkdownFileMenuAction =
  | "preview-media"
  | "open-in-panel"
  | "open-full-size"
  | "open"
  | "open-in-browser"
  | "reveal"
  | "download"
  | "copy-relative"
  | "copy-full"
  | "copy-path-only";

export function markdownFileMenuItems(input: {
  hasLine: boolean;
  workspaceRelativePath: string | null;
  canOpenMedia: boolean;
  canOpenInPanel: boolean;
  openInEditorLabel: string | null;
  canOpenInBrowser: boolean;
  revealLabel: string | null;
  canDownload: boolean;
}): ContextMenuItem<MarkdownFileMenuAction>[] {
  const actions: ReadonlyArray<readonly [MarkdownFileMenuAction, string | null | false]> = [
    ["preview-media", input.canOpenMedia && "Preview media"],
    ["open-in-panel", input.canOpenInPanel && "Open in files viewer"],
    ["open-full-size", input.canOpenInPanel && "Open full size"],
    ["open", input.openInEditorLabel],
    ["open-in-browser", input.canOpenInBrowser && "Open in integrated browser"],
    ["reveal", input.revealLabel],
    ["download", input.canDownload && "Download to this device"],
  ];
  const copies: ReadonlyArray<readonly [MarkdownFileMenuAction, string | null | false]> = [
    ["copy-relative", input.workspaceRelativePath !== null && "Copy relative path"],
    ["copy-full", "Copy full path"],
    ["copy-path-only", input.hasLine && "Copy full path without line"],
  ];
  const present = (entries: typeof actions) =>
    entries.flatMap(([id, label]) => (label ? [{ id, label }] : []));
  const opens = present(actions);
  const [firstCopy, ...otherCopies] = present(copies);
  return [
    ...opens,
    ...(firstCopy ? [{ ...firstCopy, separatorBefore: opens.length > 0 }] : []),
    ...otherCopies,
  ];
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

export function shouldUseMarkdownFileBrowserPrimaryAction(input: {
  iconPath: string;
  canOpenInEditor: boolean;
  canOpenInBrowser: boolean;
  canOpenInPanel: boolean;
}): boolean {
  return (
    input.canOpenInBrowser &&
    (shouldOpenMarkdownFileLinkInBrowserByDefault(input.iconPath) ||
      (!input.canOpenInEditor && !input.canOpenInPanel))
  );
}
