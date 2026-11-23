// Fork: what a chat file link's "Download to this device" says when it fails (#17). The
// download asks the server for the file as a "media-file" asset, so a missing file came back as
// "Media file was not found." even for a text file, without saying where the server looked.
import { AssetWorkspaceAssetNotFoundError } from "@t3tools/contracts";
import * as Schema from "effect/Schema";

const isAssetNotFoundError = Schema.is(AssetWorkspaceAssetNotFoundError);

/**
 * The error a failed download reports. A file the server does not have names the path the server
 * checked, and the machine when the viewer is on another one (the same wording upstream's media
 * preview uses); any other failure is reported as it came.
 */
export function forkMarkdownDownloadError(
  cause: unknown,
  input: { requestedPath: string; machine: string | null },
): unknown {
  if (!isAssetNotFoundError(cause)) return cause;
  const path = cause.checkedPath ?? input.requestedPath;
  return new Error(
    input.machine === null
      ? `The file was not found at ${path}.`
      : `The file was not found on ${input.machine} at ${path}.`,
  );
}
