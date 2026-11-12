/**
 * The GitHub repository whose releases a build updates from, baked in at build time.
 *
 * Empty means the upstream default in `@t3tools/shared/cliRelease`. A fork's release workflow sets
 * `T3CODE_RELEASE_REPOSITORY=<owner>/<repo>` so its builds look only at the fork's releases: the
 * Update button and `t3 update` can then never install an upstream build over the fork.
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
  const configured = env[RELEASE_REPOSITORY_ENV]?.trim() ?? "";
  if (configured === "") return "";
  const [owner, name, ...rest] = configured.split("/");
  if (!isValidSegment(owner) || !isValidSegment(name) || rest.length > 0) {
    throw new Error(`${RELEASE_REPOSITORY_ENV} must be "<owner>/<repo>", got "${configured}".`);
  }
  return configured;
}

/** The bundler `define` entry every build that bundles `@t3tools/shared/cliRelease` carries. */
export function releaseRepositoryDefine(
  env: Readonly<Record<string, string | undefined>> = process.env,
): Record<string, string> {
  return { __T3CODE_RELEASE_REPOSITORY__: JSON.stringify(resolveReleaseRepository(env)) };
}
