// Fork: a chat file link that names the environment holding the file. It is a plain absolute
// path under a reserved first folder, so upstream's Markdown pipeline renders it as a file link
// and the fork's link actions (apps/web forkEnvironmentFileLink.ts) read the environment from it.
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";

import { EnvironmentId, ThreadId } from "./baseSchemas.ts";

const ENVIRONMENT_FILE_LINK_PREFIX = "/t3-environment/";

/** What `t3_environment_read` tells an agent, so it can link a file on that environment. */
export const ENVIRONMENT_FILE_LINK_AGENT_HINT = `To link a file on this environment in a reply on another environment, write \`[name](${ENVIRONMENT_FILE_LINK_PREFIX}<environmentId>/<absolute path>)\` with this result's environmentId; the user can then download it from here. The path must be absolute (\`/home/me/a.txt\`, \`C:/Users/me/a.txt\`), never \`~\` or relative. Files on the reply's own environment keep plain absolute paths.`;

/**
 * `assets.createUrl` needs a thread id, but for an absolute path the server serves the file
 * without looking the thread up, so the file's environment need not have the message's thread.
 */
export const ENVIRONMENT_FILE_LINK_THREAD_ID = ThreadId.make("environment-file-link");

export interface EnvironmentFileLink {
  readonly environmentId: EnvironmentId;
  /**
   * The absolute path on that environment: `/var/log/app.log`, or `C:/Users/a/notes.txt`. A path
   * that starts at the home folder (`~/notes.txt`) is kept as written, so it can be refused by name.
   */
  readonly path: string;
}

const decodeEnvironmentId = Schema.decodeUnknownOption(EnvironmentId);
// Environments issue UUIDs; no slash, backslash, percent sign or space can be part of one.
const ENVIRONMENT_ID_CHARACTERS = /^[A-Za-z0-9._:-]+$/;
const WINDOWS_DRIVE_PATH = /^[A-Za-z]:[\\/]/;

/** The environment and path a file link's resolved path names, or null for any other path. */
export function parseEnvironmentFileLinkPath(filePath: string): EnvironmentFileLink | null {
  if (!filePath.startsWith(ENVIRONMENT_FILE_LINK_PREFIX)) return null;
  const rest = filePath.slice(ENVIRONMENT_FILE_LINK_PREFIX.length);
  const slash = rest.indexOf("/");
  if (slash <= 0) return null;
  const rawId = rest.slice(0, slash);
  if (!ENVIRONMENT_ID_CHARACTERS.test(rawId)) return null;
  const environmentId = decodeEnvironmentId(rawId);
  // Leading slashes go, so a doubled slash never becomes a network path.
  const path = rest.slice(slash + 1).replace(/^[/\\]+/, "");
  if (Option.isNone(environmentId) || path.length === 0) return null;
  return {
    environmentId: environmentId.value,
    path: WINDOWS_DRIVE_PATH.test(path) || path.startsWith("~") ? path : `/${path}`,
  };
}

/** Whether a parsed link's path is absolute; a `~` path is not, and the link can say so. */
export function isAbsoluteEnvironmentFilePath(path: string): boolean {
  return path.startsWith("/") || WINDOWS_DRIVE_PATH.test(path);
}
