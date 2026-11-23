// Fork: a file on another environment downloads and previews through that environment's own
// connection, with the same `assets.createUrl` and file-read scope the files viewer uses there.
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own messages are English only; upstream's translation catalogs hold no fork keys. */
import { resolveAssetUrl } from "@t3tools/client-runtime/state/assets";
import {
  squashAtomCommandFailure,
  type AtomCommandResult,
} from "@t3tools/client-runtime/state/runtime";
import {
  AssetWorkspaceAssetNotFoundError,
  type AssetCreateUrlResult,
  type EnvironmentId,
  type AssetResource,
  type ThreadId,
} from "@t3tools/contracts";
import {
  parseEnvironmentFileLinkHref,
  type EnvironmentFileLink,
} from "@t3tools/shared/forkEnvironmentFileLinks";
import * as Schema from "effect/Schema";

const isAssetNotFoundError = Schema.is(AssetWorkspaceAssetNotFoundError);

/**
 * How a file link in a message of `messageEnvironmentId` resolves: `local` opens `pathHref` like
 * any path link, `remote` names a file on another environment, null is not an environment link.
 */
export function environmentFileLinkTarget(
  href: string,
  messageEnvironmentId: EnvironmentId | null,
):
  | { readonly kind: "local"; readonly pathHref: string }
  | { readonly kind: "remote"; readonly link: EnvironmentFileLink }
  | null {
  const link = parseEnvironmentFileLinkHref(href);
  if (link === null) return null;
  return link.environmentId === messageEnvironmentId
    ? { kind: "local", pathHref: link.pathHref }
    : { kind: "remote", link };
}

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

type CreateAssetUrl = (input: {
  readonly environmentId: EnvironmentId;
  readonly input: { readonly resource: AssetResource };
}) => Promise<AtomCommandResult<AssetCreateUrlResult, unknown>>;

/** The file's name and path on its environment in plain words, for a not-found failure. */
export function environmentFileErrorMessage(
  error: unknown,
  environmentId: EnvironmentId,
  path: string,
  label: (environmentId: EnvironmentId) => string | null,
): string {
  if (isAssetNotFoundError(error)) {
    return `The file was not found on ${label(environmentId) ?? environmentId} at ${error.checkedPath ?? path}.`;
  }
  return error instanceof Error && error.message ? error.message : "An error occurred.";
}

/**
 * A signed download URL for `path` on the link's environment. `threadId` fills the asset's required
 * field: for an absolute path the server serves the file without looking the thread up, so the
 * message's thread need not exist there.
 */
export async function environmentFileDownloadUrl(input: {
  readonly environmentId: EnvironmentId;
  readonly path: string;
  readonly threadId: ThreadId;
  readonly readers: EnvironmentFileAccessReaders;
  readonly createAssetUrl: CreateAssetUrl;
}): Promise<string> {
  const access = environmentFileAccess(input.environmentId, input.readers);
  if ("unavailable" in access) throw new Error(access.unavailable);
  const result = await input.createAssetUrl({
    environmentId: input.environmentId,
    input: {
      resource: {
        _tag: "media-file",
        threadId: input.threadId,
        path: input.path,
        disposition: "attachment",
      },
    },
  });
  if (result._tag === "Failure") {
    throw new Error(
      environmentFileErrorMessage(
        squashAtomCommandFailure(result),
        input.environmentId,
        input.path,
        input.readers.label,
      ),
    );
  }
  const url = resolveAssetUrl(access.httpBaseUrl, result.value.relativeUrl);
  if (!url) throw new Error("Reconnect to the environment and try again.");
  return url;
}
