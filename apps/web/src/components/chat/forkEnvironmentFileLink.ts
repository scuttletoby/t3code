// Fork: a chat file link under `/t3-environment/<environmentId>/` downloads through that
// environment's own connection, with the same `assets.createUrl` and file-read scope there.
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

import type { MarkdownFileLinkMeta } from "../../markdown-links";
import { markdownFileCopyPaths } from "../../markdownFileHostPath";

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

/** A signed download URL for `path` on `environmentId`, or an error that names the environment. */
export async function environmentFileDownloadUrl(input: {
  readonly environmentId: EnvironmentId;
  readonly path: string;
  readonly readers: EnvironmentFileAccessReaders;
  readonly createAssetUrl: CreateAssetUrl;
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
    input: {
      resource: {
        _tag: "media-file",
        threadId: ENVIRONMENT_FILE_LINK_THREAD_ID,
        path,
        disposition: "attachment",
      },
    },
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

/**
 * The props that replace upstream's on a file link naming an environment, or null for any other
 * link. A click (and the menu's one action) downloads the file from that environment; actions
 * that would look for the path on the message's environment or on this device are left out.
 */
export function environmentFileLinkProps(
  fileLinkMeta: MarkdownFileLinkMeta,
  deps: {
    readonly readers: EnvironmentFileAccessReaders;
    readonly createAssetUrl: CreateAssetUrl;
    readonly downloadFile: (url: string, fileName: string) => Promise<void>;
  },
) {
  const link = parseEnvironmentFileLinkPath(fileLinkMeta.filePath);
  if (link === null) return null;
  const { environmentId, path } = link;
  const name = environmentFileLinkName(environmentId, deps.readers);
  return {
    environmentId: null,
    // The chip names the environment, so a file elsewhere does not look local.
    label: `${fileLinkMeta.basename} on ${name}`,
    href: path,
    targetPath: path,
    iconPath: path,
    workspaceRelativePath: null,
    panelPath: null,
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
    onOpenInPanel: () => undefined,
    onOpenFullSize: undefined,
    onDownload: undefined,
    onOpenMedia: undefined,
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
