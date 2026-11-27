/**
 * Fork: a project's `.t3code/issues.json`, committed in its repository. `repositories` names the
 * git-host repositories (scopes, `owner/repo` on GitHub) the project's Issues page lists and its
 * issue tools may write to. It replaces the ones T3 guesses from git remotes, which for a fork
 * checkout include its upstream. A project without the file, or without the key, keeps the guess.
 */
import * as Schema from "effect/Schema";

/** One repository on the project's git host, as its issue URLs name it (`owner/repo`). */
const IssuesConfigRepository = Schema.String.check(
  Schema.isPattern(/^[^\s/]+(?:\/[^\s/]+)+$/),
  Schema.isMaxLength(200),
);

export const ForkIssuesConfig = Schema.Struct({
  repositories: Schema.optional(Schema.Array(IssuesConfigRepository).check(Schema.isMaxLength(50))),
});
export type ForkIssuesConfig = typeof ForkIssuesConfig.Type;

/** Where the file sits, from the project's workspace root. */
export const FORK_ISSUES_CONFIG_PATH = [".t3code", "issues.json"] as const;
