// Fork: a chat file link under `/t3-environment/<environmentId>/` downloads, previews media and
// opens in the files viewer through that environment's own connection, with the same
// `assets.createUrl` and file-read scope there. Nothing is requested before a click.
import { resolveAssetUrl } from "@t3tools/client-runtime/state/assets";
import {
  squashAtomCommandFailure,
  type AtomCommandResult,
} from "@t3tools/client-runtime/state/runtime";
import {
  AssetWorkspaceAssetNotFoundError,
  ENVIRONMENT_FILE_LINK_THREAD_ID,
  isAbsoluteEnvironmentFilePath,
  parseEnvironmentFileLinkPath,
  type AssetCreateUrlResult,
  type AssetResource,
  type EnvironmentId,
} from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import { AsyncResult } from "effect/reactivity";
import * as Schema from "effect/Schema";
import { mediaMimeTypeFromExtension } from "@t3tools/shared/mediaPreview";

import type { MarkdownFileLinkMeta } from "../../markdown-links";
import { markdownFileCopyPaths } from "../../markdownFileHostPath";
import type { ExpandedImagePreview } from "./ExpandedImagePreview";

const isAssetNotFoundError = Schema.is(AssetWorkspaceAssetNotFoundError);

export const DOWNLOAD_LABEL = "Download to this device";
const UNKNOWN_ENVIRONMENT = "unknown environment";

export interface EnvironmentFileAccessReaders {
  /** This client's prepared connection to the environment, if it has one. */
  readonly httpBaseUrl: (environmentId: EnvironmentId) => string | null;
  readonly canReadFiles: (environmentId: EnvironmentId) => boolean;
  /** The environment's saved name on this client, or null when this client does not know it. */
  readonly label: (environmentId: EnvironmentId) => string | null;
  /** Whether this client is (re)connecting to it, so a missing connection or scope may pass. */
  readonly reconnecting: (environmentId: EnvironmentId) => boolean;
}

/** The name to show for an environment; a link's id is never shown, as the link may be crafted. */
function environmentFileLinkName(
  environmentId: EnvironmentId,
  readers: Pick<EnvironmentFileAccessReaders, "label">,
): string {
  return readers.label(environmentId) ?? UNKNOWN_ENVIRONMENT;
}

/** The environment's connection base URL, or a message that names it and says what is missing. */
export function environmentFileAccess(
  environmentId: EnvironmentId,
  readers: EnvironmentFileAccessReaders,
): { readonly httpBaseUrl: string } | { readonly unavailable: string } {
  const httpBaseUrl = readers.httpBaseUrl(environmentId);
  if (httpBaseUrl !== null && readers.canReadFiles(environmentId)) return { httpBaseUrl };
  const label = readers.label(environmentId);
  if (label === null) {
    return {
      unavailable:
        "This file is on an environment this device does not know. Add it in Settings > Connections, then try again.",
    };
  }
  if (readers.reconnecting(environmentId)) {
    return { unavailable: `This device is reconnecting to ${label}. Try again in a moment.` };
  }
  if (httpBaseUrl === null) {
    return {
      unavailable: `This file is on ${label}, which this device is not connected to. Connect to ${label} in Settings > Connections, then try again.`,
    };
  }
  return { unavailable: `This device may not read files on ${label}.` };
}

export type CreateAssetUrl = (input: {
  readonly environmentId: EnvironmentId;
  readonly input: { readonly resource: AssetResource };
}) => Promise<AtomCommandResult<AssetCreateUrlResult, unknown>>;

/** The `media-file` resource for `path` on another environment; inline unless `attachment`. */
export function environmentFileResource(
  path: string,
  disposition: "inline" | "attachment",
): Extract<AssetResource, { _tag: "media-file" }> {
  return {
    _tag: "media-file",
    threadId: ENVIRONMENT_FILE_LINK_THREAD_ID,
    path,
    ...(disposition === "attachment" ? { disposition } : {}),
  };
}

/**
 * A signed URL for `path` on `environmentId` (a download unless `inline`), or an error that names
 * the environment. Asks nothing when this device has no connection or file read there.
 */
export async function environmentFileDownloadUrl(input: {
  readonly environmentId: EnvironmentId;
  readonly path: string;
  readonly readers: EnvironmentFileAccessReaders;
  readonly createAssetUrl: CreateAssetUrl;
  readonly inline?: boolean;
}): Promise<string> {
  const { environmentId, path, readers } = input;
  const name = environmentFileLinkName(environmentId, readers);
  if (!isAbsoluteEnvironmentFilePath(path)) {
    throw new Error(
      `A link to a file on ${name} needs the file's absolute path; ${path} starts at a home folder.`,
    );
  }
  const access = environmentFileAccess(environmentId, readers);
  if ("unavailable" in access) throw new Error(access.unavailable);
  const result = await input.createAssetUrl({
    environmentId,
    input: { resource: environmentFileResource(path, input.inline ? "inline" : "attachment") },
  });
  if (result._tag === "Failure") {
    const cause = squashAtomCommandFailure(result);
    if (isAssetNotFoundError(cause)) {
      throw new Error(`The file was not found on ${name} at ${cause.checkedPath ?? path}.`);
    }
    throw cause instanceof Error ? cause : new Error("An error occurred.");
  }
  const url = resolveAssetUrl(access.httpBaseUrl, result.value.relativeUrl);
  if (!url) throw new Error("Reconnect to the environment and try again.");
  return url;
}

/** Whether a link to `name` previews as an image or a video, as upstream's media preview does. */
function environmentFileMediaKind(name: string): "image" | "video" | null {
  const dot = name.lastIndexOf(".");
  const mimeType = dot === -1 ? null : mediaMimeTypeFromExtension(name.slice(dot));
  if (mimeType === null) return null;
  return mimeType.startsWith("video/") ? "video" : "image";
}

/** Upstream's media viewer item for `path` on another environment, signed by that environment. */
async function environmentFileMediaPreview(input: {
  readonly environmentId: EnvironmentId;
  readonly path: string;
  readonly name: string;
  readonly kind: "image" | "video";
  readonly readers: EnvironmentFileAccessReaders;
  readonly createAssetUrl: CreateAssetUrl;
}): Promise<ExpandedImagePreview> {
  const { environmentId, path, name, kind } = input;
  const src = await environmentFileDownloadUrl({ ...input, inline: true });
  const asset = { environmentId, resource: environmentFileResource(path, "inline") };
  return {
    images: [
      {
        src,
        name,
        ...(kind === "video" ? { type: "video" as const, autoPlay: false } : {}),
        actionsSource: { kind, name, src, asset },
      },
    ],
    index: 0,
  };
}

/** Where a link naming an environment previews and opens; both stay unset without a chat. */
export interface EnvironmentFileLinkChat {
  /** Upstream's media viewer of the message. */
  readonly expandMedia: (preview: ExpandedImagePreview) => void;
  /** Opens the link's path (`/t3-environment/<id>/...`) in the thread's files viewer. */
  readonly openInPanel: (linkPath: string, line: number | undefined, maximize: boolean) => void;
  readonly reportMediaError: (error: unknown) => void;
}

/**
 * The props that replace upstream's on a file link naming an environment, or null for any other
 * link. A click previews an image or a video, or opens any other file in the files viewer, both
 * from that environment; the menu also downloads it. Actions that would look for the path on the
 * message's environment or on this device are left out.
 */
export function environmentFileLinkProps(
  fileLinkMeta: MarkdownFileLinkMeta,
  deps: {
    readonly readers: EnvironmentFileAccessReaders;
    readonly createAssetUrl: CreateAssetUrl;
    readonly downloadFile: (url: string, fileName: string) => Promise<void>;
    readonly chat?: EnvironmentFileLinkChat | undefined;
  },
) {
  const link = parseEnvironmentFileLinkPath(fileLinkMeta.filePath);
  if (link === null) return null;
  const { environmentId, path } = link;
  const name = environmentFileLinkName(environmentId, deps.readers);
  const { chat } = deps;
  const mediaKind = environmentFileMediaKind(fileLinkMeta.basename);
  // The files viewer parses the environment from the link's path again (forkEnvironmentFilePanel).
  const panelPath = chat && mediaKind === null ? fileLinkMeta.filePath : null;
  return {
    environmentId: null,
    // The chip names the environment, so a file elsewhere does not look local.
    label: `${fileLinkMeta.basename} on ${name}`,
    href: path,
    targetPath: path,
    iconPath: path,
    workspaceRelativePath: null,
    panelPath,
    onOpen: async (): Promise<AtomCommandResult<void, unknown>> => {
      try {
        const url = await environmentFileDownloadUrl({ environmentId, path, ...deps });
        await deps.downloadFile(url, fileLinkMeta.basename);
        return AsyncResult.success<void>(undefined);
      } catch (error) {
        return AsyncResult.failure<void, unknown>(Cause.fail(error));
      }
    },
    openInEditorMenuLabel: DOWNLOAD_LABEL,
    onOpenInPanel: (linkPath: string, line: number | undefined) =>
      chat?.openInPanel(linkPath, line, false),
    onOpenFullSize:
      chat && panelPath ? () => chat.openInPanel(panelPath, fileLinkMeta.line, true) : undefined,
    onDownload: undefined,
    onOpenMedia:
      chat && mediaKind
        ? () => {
            environmentFileMediaPreview({
              environmentId,
              path,
              name: fileLinkMeta.basename,
              kind: mediaKind,
              ...deps,
            }).then(chat.expandMedia, chat.reportMediaError);
          }
        : undefined,
    onOpenInBrowser: undefined,
    onReveal: undefined,
    revealLabel: undefined,
    resolveDragPath: async () => path,
    resolveCopyPaths: async () =>
      markdownFileCopyPaths(
        { ...fileLinkMeta, filePath: path, targetPath: path },
        { absolutePath: path, matched: true },
        undefined,
      ),
  };
}
