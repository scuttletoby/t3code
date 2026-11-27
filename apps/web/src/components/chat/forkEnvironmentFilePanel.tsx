// Fork: the files viewer for a chat file link naming another environment
// (`/t3-environment/<environmentId>/<path>`). It reads the file through that environment's
// own connection and file-read scope (the same `assets.createUrl` as the link's download), never
// through the thread's environment, and asks nothing while this device cannot read files there.
import {
  isAbsoluteEnvironmentFilePath,
  parseEnvironmentFileLinkPath,
  type EnvironmentId,
} from "@t3tools/contracts";
import { filePreviewKind } from "@t3tools/shared/filePreview";
import { lazy, Suspense } from "react";

import type { RightPanelSurface } from "../../rightPanelStore";
import { useFilesystemReadAccess } from "../../state/filesystem";
import { usePreparedConnection } from "../../state/session";
import {
  environmentFileAccess,
  environmentFileResource,
  type EnvironmentFileAccessReaders,
} from "./forkEnvironmentFileLink";
import { useEnvironmentFileAccessReaders } from "./forkMarkdownFileLinkActions";

const AttachmentFilePreview = lazy(() =>
  import("../files/AttachmentFilePreview").then((module) => ({
    default: module.AttachmentFilePreview,
  })),
);

/**
 * Whether the files viewer surface holds a link path naming an environment. A plain boolean, not a
 * type guard: a guard would narrow every file surface out of upstream's code after the hook.
 */
export function isForkEnvironmentFile(surface: RightPanelSurface): boolean {
  return (
    surface.kind === "file" &&
    surface.attachment === undefined &&
    parseEnvironmentFileLinkPath(surface.relativePath) !== null
  );
}

// The server signs these inline; any other file is read from a download URL (as upstream's files
// viewer reads a workspace file it cannot show inline).
const INLINE_KINDS: ReadonlySet<string> = new Set(["image", "video", "audio", "pdf", "html"]);

export type EnvironmentFilePanelContent =
  | { readonly unavailable: string }
  | {
      readonly environmentId: EnvironmentId;
      readonly path: string;
      readonly name: string;
      readonly inline: boolean;
    };

/** What the files viewer shows for a link path: the file on its environment, or why it cannot. */
export function environmentFilePanelContent(
  linkPath: string,
  readers: EnvironmentFileAccessReaders,
): EnvironmentFilePanelContent | null {
  const link = parseEnvironmentFileLinkPath(linkPath);
  if (link === null) return null;
  const { environmentId, path } = link;
  if (!isAbsoluteEnvironmentFilePath(path)) {
    const label = readers.label(environmentId) ?? "unknown environment";
    return {
      unavailable: `A link to a file on ${label} needs the file's absolute path; ${path} starts at a home folder.`,
    };
  }
  const access = environmentFileAccess(environmentId, readers);
  if ("unavailable" in access) return access;
  const name = path.split(/[\\/]/).at(-1) || path;
  return { environmentId, path, name, inline: INLINE_KINDS.has(filePreviewKind({ name })) };
}

export function ForkEnvironmentFilePanel(props: { readonly surface: RightPanelSurface }) {
  const readers = useEnvironmentFileAccessReaders();
  const linkPath = props.surface.kind === "file" ? props.surface.relativePath : "";
  const environmentId = parseEnvironmentFileLinkPath(linkPath)?.environmentId;
  // Subscribed so the panel follows that environment connecting, reconnecting or losing access.
  usePreparedConnection(environmentId ?? null);
  useFilesystemReadAccess(environmentId ?? null);
  const content = environmentFilePanelContent(linkPath, readers);
  if (content === null) return null;
  if ("unavailable" in content) {
    return (
      <div role="alert" className="p-4 text-muted-foreground text-sm">
        {content.unavailable}
      </div>
    );
  }
  return (
    <EnvironmentFileView
      key={`${content.environmentId}:${content.path}`}
      environmentId={content.environmentId}
      path={content.path}
      name={content.name}
      inline={content.inline}
      origin={`File on ${readers.label(content.environmentId) ?? "unknown environment"}`}
    />
  );
}

function EnvironmentFileView(props: {
  readonly environmentId: EnvironmentId;
  readonly path: string;
  readonly name: string;
  readonly inline: boolean;
  readonly origin: string;
}) {
  // Built from primitives only: the preview keys its signed URL on this object.
  const resource = environmentFileResource(props.path, props.inline ? "inline" : "attachment");
  return (
    <Suspense fallback={null}>
      <AttachmentFilePreview
        name={props.name}
        mimeType="application/octet-stream"
        sizeBytes={0}
        asset={{ environmentId: props.environmentId, resource }}
        origin={props.origin}
      />
    </Suspense>
  );
}
