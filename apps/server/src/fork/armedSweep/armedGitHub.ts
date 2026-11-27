/**
 * Fork (issue 880): the armed PR sweep's one GitHub request: every open pull request of the swept
 * repositories (aliased searches) and, by id, the ones the last sweep followed, so a closed or
 * merged one is seen without a read of its own. Pure: build the request, read the answer.
 */
import type { ArmedCheck, ArmedPull, CheckState } from "./armedJudge.ts";

/** GitHub refuses a search query longer than this. */
const SEARCH_QUERY_MAX = 256;
const SEARCH_PAGE = 50;
/** GitHub's own cap on `nodes(ids:)`. */
export const NODES_MAX = 100;
const CHECKS_MAX = 100;

const PULL_FIELDS = `
  id number url title state merged headRefOid mergeable
  repository { nameWithOwner }
  autoMergeRequest { enabledAt }
  commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: ${CHECKS_MAX}) { nodes {
    __typename
    ... on CheckRun { name status conclusion completedAt }
    ... on StatusContext { context state createdAt }
  } } } } } }`;

/** Repository names GitHub accepts in a `repo:` qualifier; anything else is never sent. */
const REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

/** The search queries for these repositories: as few as fit GitHub's query length. */
export function searchQueries(repositories: ReadonlyArray<string>): ReadonlyArray<string> {
  const base = "is:pr is:open archived:false";
  const queries: string[] = [];
  let current = base;
  for (const repository of [...new Set(repositories)]
    .filter((name) => REPOSITORY.test(name))
    .toSorted()) {
    const part = ` repo:${repository}`;
    if (current !== base && current.length + part.length > SEARCH_QUERY_MAX) {
      queries.push(current);
      current = base;
    }
    current += part;
  }
  if (current !== base) queries.push(current);
  return queries;
}

export interface SearchCursor {
  readonly query: string;
  readonly after: string | null;
}

/** One GraphQL document: each search at its cursor, and the followed pull requests by id. */
export function buildRequest(
  searches: ReadonlyArray<SearchCursor>,
  ids: ReadonlyArray<string>,
): string {
  const parts = searches.map(
    ({ query, after }, index) =>
      `s${index}: search(type: ISSUE, first: ${SEARCH_PAGE}, query: ${JSON.stringify(query)}${
        after === null ? "" : `, after: ${JSON.stringify(after)}`
      }) { pageInfo { hasNextPage endCursor } nodes { ... on PullRequest { ${PULL_FIELDS} } } }`,
  );
  if (ids.length > 0) {
    parts.push(
      `followed: nodes(ids: ${JSON.stringify(ids.slice(0, NODES_MAX))}) { ... on PullRequest { ${PULL_FIELDS} } }`,
    );
  }
  return `query { ${parts.join("\n")} }`;
}

export interface ParsedAnswer {
  readonly pulls: ReadonlyArray<ArmedPull>;
  /** Searches with another page: their next cursors. */
  readonly more: ReadonlyArray<SearchCursor>;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const timeOf = (value: unknown): number | null => {
  if (typeof value !== "string") return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
};

const CHECK_RUN_FAILED = new Set([
  "FAILURE",
  "CANCELLED",
  "TIMED_OUT",
  "ACTION_REQUIRED",
  "STARTUP_FAILURE",
  "STALE",
]);

function checkOf(raw: unknown): ArmedCheck | null {
  if (!isRecord(raw)) return null;
  if (raw.__typename === "CheckRun" && typeof raw.name === "string") {
    if (raw.status !== "COMPLETED") return { name: raw.name, state: "pending", completedAt: null };
    const state: CheckState = CHECK_RUN_FAILED.has(String(raw.conclusion)) ? "failure" : "success";
    return { name: raw.name, state, completedAt: timeOf(raw.completedAt) };
  }
  if (raw.__typename === "StatusContext" && typeof raw.context === "string") {
    const state: CheckState =
      raw.state === "SUCCESS"
        ? "success"
        : raw.state === "FAILURE" || raw.state === "ERROR"
          ? "failure"
          : "pending";
    return {
      name: raw.context,
      state,
      completedAt: state === "pending" ? null : timeOf(raw.createdAt),
    };
  }
  return null;
}

/** One pull request node, or null for anything not shaped as asked (never guessed). */
export function pullOf(raw: unknown): ArmedPull | null {
  if (!isRecord(raw)) return null;
  const repository = isRecord(raw.repository) ? raw.repository.nameWithOwner : undefined;
  if (
    typeof raw.id !== "string" ||
    typeof raw.number !== "number" ||
    typeof raw.url !== "string" ||
    typeof raw.headRefOid !== "string" ||
    typeof repository !== "string"
  ) {
    return null;
  }
  const commit =
    isRecord(raw.commits) && Array.isArray(raw.commits.nodes) ? raw.commits.nodes[0] : null;
  const rollup =
    isRecord(commit) && isRecord(commit.commit) ? commit.commit.statusCheckRollup : null;
  const contexts = isRecord(rollup) && isRecord(rollup.contexts) ? rollup.contexts.nodes : null;
  const checks = Array.isArray(contexts)
    ? contexts.map(checkOf).filter((check): check is ArmedCheck => check !== null)
    : [];
  return {
    id: raw.id,
    repository,
    number: raw.number,
    url: raw.url,
    title: typeof raw.title === "string" ? raw.title : "",
    state: raw.merged === true ? "merged" : raw.state === "OPEN" ? "open" : "closed",
    armed: isRecord(raw.autoMergeRequest),
    head: raw.headRefOid,
    mergeable:
      raw.mergeable === "CONFLICTING"
        ? "conflicting"
        : raw.mergeable === "MERGEABLE"
          ? "mergeable"
          : "unknown",
    checks,
  };
}

/**
 * The answer's pull requests, each once (a search and the followed list can both have one), and
 * the searches with another page. An answer without its `data`, or with a GraphQL error, is
 * refused whole: a partial list would read the missing pull requests as gone.
 */
export function parseAnswer(
  searches: ReadonlyArray<SearchCursor>,
  answer: unknown,
): ParsedAnswer | { readonly error: string } {
  if (!isRecord(answer) || !isRecord(answer.data)) return { error: "GitHub gave no data" };
  // A followed id GitHub no longer resolves (a deleted repository) is an error on `followed`
  // only: that pull request reads as unknown. Any other error refuses the answer.
  const refused = (Array.isArray(answer.errors) ? answer.errors : []).find(
    (error) => !(isRecord(error) && Array.isArray(error.path) && error.path[0] === "followed"),
  );
  if (refused !== undefined) {
    const message = isRecord(refused) && typeof refused.message === "string" ? refused.message : "";
    return { error: `GitHub refused the list: ${message.slice(0, 200)}` };
  }
  const data = answer.data;
  const byId = new Map<string, ArmedPull>();
  const more: SearchCursor[] = [];
  for (const [index, search] of searches.entries()) {
    const result = data[`s${index}`];
    if (!isRecord(result) || !Array.isArray(result.nodes)) return { error: "GitHub gave no list" };
    for (const node of result.nodes) {
      const pull = pullOf(node);
      if (pull !== null) byId.set(pull.id, pull);
    }
    const info = isRecord(result.pageInfo) ? result.pageInfo : {};
    if (info.hasNextPage === true && typeof info.endCursor === "string") {
      more.push({ query: search.query, after: info.endCursor });
    }
  }
  if (Array.isArray(data.followed)) {
    for (const node of data.followed) {
      const pull = pullOf(node);
      if (pull !== null && !byId.has(pull.id)) byId.set(pull.id, pull);
    }
  }
  return { pulls: [...byId.values()], more };
}
