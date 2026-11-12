/**
 * The repository a build updates from, for the server, CLI and desktop SSH installs. It is the one
 * upstream already uses for the desktop updater feed and release links (docs/operations/release.md):
 * T3CODE_DESKTOP_UPDATE_REPOSITORY, else GITHUB_REPOSITORY, else "" for upstream.
 */
export const RELEASE_REPOSITORY_ENV = "T3CODE_DESKTOP_UPDATE_REPOSITORY";
// The setting's first name in this fork; still accepted only when it names the same repository.
const FORMER_RELEASE_REPOSITORY_ENV = "T3CODE_RELEASE_REPOSITORY";

const SEGMENT = /^[A-Za-z0-9._-]+$/u;

function isValidSegment(value: string | undefined): value is string {
  return value !== undefined && value !== "." && value !== ".." && SEGMENT.test(value);
}

/** The configured repository, or "" for the upstream default. Throws on a malformed value or a
 * disagreeing former setting, so no build ships the desktop and the server on different feeds. */
export function resolveReleaseRepository(
  env: Readonly<Record<string, string | undefined>> = process.env,
): string {
  // The same order as apps/web/vite.config.ts and resolveGitHubPublishConfig read them.
  const configured = env[RELEASE_REPOSITORY_ENV]?.trim() || env.GITHUB_REPOSITORY?.trim() || "";
  const former = env[FORMER_RELEASE_REPOSITORY_ENV]?.trim() || "";
  if (former !== "" && former !== configured) {
    throw new Error(
      `${FORMER_RELEASE_REPOSITORY_ENV}="${former}" disagrees with the repository the desktop ` +
        `updater uses ("${configured || "pingdotgg/t3code"}"). Set ${RELEASE_REPOSITORY_ENV} instead.`,
    );
  }
  if (configured === "") return "";
  const [owner, name, ...rest] = configured.split("/");
  if (!isValidSegment(owner) || !isValidSegment(name) || rest.length > 0) {
    throw new Error(
      `${RELEASE_REPOSITORY_ENV} (or GITHUB_REPOSITORY) must be "<owner>/<repo>", got "${configured}".`,
    );
  }
  return configured;
}

/** The bundler `define` entry every build that bundles `@t3tools/shared/cliRelease` carries. */
export function releaseRepositoryDefine(
  env: Readonly<Record<string, string | undefined>> = process.env,
): Record<string, string> {
  return { __T3CODE_RELEASE_REPOSITORY__: JSON.stringify(resolveReleaseRepository(env)) };
}
