// Fork: a chat file link under `/t3-environment/<environmentId>/` downloads through that
// environment's own connection, with the same `assets.createUrl` and file-read scope there.
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own messages are English only; upstream's translation catalogs hold no fork keys. */
import { resolveAssetUrl } from "@t3tools/client-runtime/state/assets";
import {
  squashAtomCommandFailure,
  type AtomCommandResult,
} from "@t3tools/client-runtime/state/runtime";
import {
  AssetWorkspaceAssetNotFoundError,
  parseEnvironmentFileLinkPath,
  ThreadId,
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

/**
 * `assets.createUrl` needs a thread id, but for an absolute path the server serves the file
 * without looking the thread up, so the file's environment need not have the message's thread.
 */
const FILE_LINK_THREAD_ID = ThreadId.make("environment-file-link");

export const DOWNLOAD_LABEL = "Download to this device";

export interface EnvironmentFileAccessReaders {
  /** This client's prepared connection to the environment, if it has one. */
  readonly httpBaseUrl: (environmentId: EnvironmentId) => string | null;
  readonly canReadFiles: (environmentId: EnvironmentId) => boolean;
  /** The environment's saved name on this client. */
  readonly label: (environmentId: EnvironmentId) => string | null;
}

/** The environment's connection base URL, or a message that names it and says what is missing. */
export function environmentFileAccess(
  environmentId: EnvironmentId,
  readers: EnvironmentFileAccessReaders,
): { readonly httpBaseUrl: string } | { readonly unavailable: string } {
  const name = readers.label(environmentId) ?? environmentId;
  const httpBaseUrl = readers.httpBaseUrl(environmentId);
  if (httpBaseUrl === null) {
    return {
      unavailable: `This file is on ${name}, which this device is not connected to. Connect to ${name} in Settings > Connections, then try again.`,
    };
  }
  if (!readers.canReadFiles(environmentId)) {
    return { unavailable: `This device may not read files on ${name}.` };
  }
  return { httpBaseUrl };
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
  const access = environmentFileAccess(environmentId, readers);
  if ("unavailable" in access) throw new Error(access.unavailable);
  const result = await input.createAssetUrl({
    environmentId,
    input: {
      resource: {
        _tag: "media-file",
        threadId: FILE_LINK_THREAD_ID,
        path,
        disposition: "attachment",
      },
    },
  });
  if (result._tag === "Failure") {
    const cause = squashAtomCommandFailure(result);
    if (isAssetNotFoundError(cause)) {
      throw new Error(
        `The file was not found on ${readers.label(environmentId) ?? environmentId} at ${cause.checkedPath ?? path}.`,
      );
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
  return {
    environmentId: null,
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
