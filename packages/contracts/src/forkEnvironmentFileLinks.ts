// Fork: a chat file link that names the environment holding the file. It is a plain absolute
// path under a reserved first folder, so upstream's Markdown pipeline renders it as a file link
// and the fork's link actions (apps/web forkEnvironmentFileLink.ts) read the environment from it.
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";

import { EnvironmentId } from "./baseSchemas.ts";

const ENVIRONMENT_FILE_LINK_PREFIX = "/t3-environment/";

/** What `t3_environment_read` tells an agent, so it can link a file on that environment. */
export const ENVIRONMENT_FILE_LINK_AGENT_HINT = `To link a file on this environment in a reply on another environment, write \`[name](${ENVIRONMENT_FILE_LINK_PREFIX}<environmentId>/<absolute path>)\` with this result's environmentId; the user can then download it from here. Files on the reply's own environment keep plain absolute paths.`;

export interface EnvironmentFileLink {
  readonly environmentId: EnvironmentId;
  /** The absolute path on that environment: `/var/log/app.log`, or `C:/Users/a/notes.txt`. */
  readonly path: string;
}

const decodeEnvironmentId = Schema.decodeUnknownOption(EnvironmentId);
const WINDOWS_DRIVE_PATH = /^[A-Za-z]:[\\/]/;

/** The environment and path a file link's resolved path names, or null for any other path. */
export function parseEnvironmentFileLinkPath(filePath: string): EnvironmentFileLink | null {
  if (!filePath.startsWith(ENVIRONMENT_FILE_LINK_PREFIX)) return null;
  const rest = filePath.slice(ENVIRONMENT_FILE_LINK_PREFIX.length);
  const slash = rest.indexOf("/");
  if (slash <= 0) return null;
  const environmentId = decodeEnvironmentId(rest.slice(0, slash));
  // Leading slashes go, so a doubled slash never becomes a network path.
  const path = rest.slice(slash + 1).replace(/^[/\\]+/, "");
  if (Option.isNone(environmentId) || path.length === 0) return null;
  return {
    environmentId: environmentId.value,
    path: WINDOWS_DRIVE_PATH.test(path) ? path : `/${path}`,
  };
}
