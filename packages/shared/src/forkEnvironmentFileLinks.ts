// Fork: a chat file link that names the environment holding the file, the way thread links
// do: `t3-file://v1/<environmentId>/<absolute path>`. The sanitizer keeps it, the web renderer
// opens it through that environment's connection, and the agent tool docs say how to write it.
import { EnvironmentId } from "@t3tools/contracts";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";

export const ENVIRONMENT_FILE_LINK_PROTOCOL = "t3-file";
const ENVIRONMENT_FILE_LINK_PREFIX = `${ENVIRONMENT_FILE_LINK_PROTOCOL}://v1/`;

/** How an agent links a file on another environment; part of the linked-environments result. */
export const ENVIRONMENT_FILE_LINK_AGENT_HINT =
  "Link a file on one of these as `[name](t3-file://v1/<environmentId>/<absolute path>)` so the user can download or preview it from there. Files on this environment keep plain absolute paths.";

const decodeEnvironmentId = Schema.decodeUnknownOption(EnvironmentId);

export interface EnvironmentFileLink {
  readonly environmentId: EnvironmentId;
  /** The file as a plain absolute path link writes it: `/abs/a.ts:4`, `/C:/x.txt`. */
  readonly pathHref: string;
}

/** The environment and path a `t3-file://v1/<environmentId>/<path>` link names, or null. */
export function parseEnvironmentFileLinkHref(href: string): EnvironmentFileLink | null {
  if (!href.toLowerCase().startsWith(ENVIRONMENT_FILE_LINK_PREFIX)) return null;
  const rest = href.slice(ENVIRONMENT_FILE_LINK_PREFIX.length);
  const slash = rest.indexOf("/");
  if (slash <= 0) return null;
  const environmentId = Option.getOrNull(decodeEnvironmentId(rest.slice(0, slash)));
  // `<id>//abs` and `<id>/abs` name the same file; a doubled slash must not become a network path.
  const path = rest.slice(slash + 1).replace(/^[\\/]+/, "");
  if (environmentId === null || path.length === 0) return null;
  return { environmentId, pathHref: `/${path}` };
}
