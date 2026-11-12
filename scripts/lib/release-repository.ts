/**
 * The repository a build updates from: T3CODE_RELEASE_REPOSITORY, else an Actions build's own
 * GITHUB_REPOSITORY (reusable workflows drop the caller's env), else "" for upstream. Keeps a
 * fork's Update button and `t3 update` on the fork's releases.
 */
export const RELEASE_REPOSITORY_ENV = "T3CODE_RELEASE_REPOSITORY";

const SEGMENT = /^[A-Za-z0-9._-]+$/u;

function isValidSegment(value: string | undefined): value is string {
  return value !== undefined && value !== "." && value !== ".." && SEGMENT.test(value);
}

/** The configured repository, or "" for the upstream default. Throws on a malformed value, so a
 * typo fails the build instead of quietly shipping a build that updates from upstream. */
export function resolveReleaseRepository(
  env: Readonly<Record<string, string | undefined>> = process.env,
): string {
  const configured =
    env[RELEASE_REPOSITORY_ENV]?.trim() ||
    (env.GITHUB_ACTIONS === "true" ? env.GITHUB_REPOSITORY?.trim() : "") ||
    "";
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
