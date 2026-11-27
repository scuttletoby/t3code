/**
 * Fork (issue 880): the armed PR sweep's GitHub request and answer. Questions: do the searches fit
 * GitHub's query length and send only real repository names? Is an answer with an error refused
 * whole (a partial list would read pull requests as gone), except an id GitHub no longer resolves?
 */
import { describe, expect, it } from "@effect/vitest";

import { buildRequest, parseAnswer, pullOf, searchQueries } from "./armedGitHub.ts";

const node = (overrides: Record<string, unknown> = {}) => ({
  id: "PR_1",
  number: 1,
  url: "https://github.com/acme/app/pull/1",
  title: "One",
  state: "OPEN",
  merged: false,
  headRefOid: "a".repeat(40),
  mergeable: "MERGEABLE",
  repository: { nameWithOwner: "acme/app" },
  autoMergeRequest: { enabledAt: "2026-11-27T00:00:00Z" },
  commits: { nodes: [] },
  ...overrides,
});

describe("searchQueries", () => {
  it("splits repositories so each query fits 256 characters, each repository once", () => {
    const repositories = Array.from(
      { length: 30 },
      (_, index) => `owner-${index}/repository-${index}`,
    );
    const queries = searchQueries([...repositories, repositories[0]!]);
    expect(queries.length).toBeGreaterThan(1);
    for (const query of queries) expect(query.length).toBeLessThanOrEqual(256);
    expect(queries.join(" ").match(/repo:/g)).toHaveLength(30);
  });

  it("never sends a name that is not owner/name", () => {
    expect(searchQueries(['acme/app" OR x', "acme/app"])).toEqual([
      "is:pr is:open archived:false repo:acme/app",
    ]);
    expect(searchQueries([])).toEqual([]);
  });
});

describe("parseAnswer", () => {
  const searches = [{ query: "is:pr repo:acme/app", after: null }];

  it("reads pull requests and the next page of a search", () => {
    const parsed = parseAnswer(searches, {
      data: { s0: { pageInfo: { hasNextPage: true, endCursor: "c1" }, nodes: [node()] } },
    });
    expect(parsed).toMatchObject({
      pulls: [{ id: "PR_1", armed: true, state: "open", mergeable: "mergeable" }],
      more: [{ query: "is:pr repo:acme/app", after: "c1" }],
    });
  });

  it("refuses an answer with an error or without its list", () => {
    expect(
      parseAnswer(searches, { errors: [{ message: "rate limited" }], data: null }),
    ).toHaveProperty("error");
    expect(
      parseAnswer(searches, {
        errors: [{ message: "boom", path: ["s0"] }],
        data: { s0: { nodes: [] } },
      }),
    ).toHaveProperty("error");
    expect(parseAnswer(searches, { data: {} })).toHaveProperty("error");
  });

  it("takes an unresolved followed id as unknown, not as a refusal", () => {
    const parsed = parseAnswer(searches, {
      errors: [{ message: "Could not resolve to a node", path: ["followed", 0] }],
      data: { s0: { nodes: [] }, followed: [null, node({ id: "PR_2", state: "CLOSED" })] },
    });
    expect(parsed).toMatchObject({ pulls: [{ id: "PR_2", state: "closed" }] });
  });

  it("reads check runs and status contexts", () => {
    const pull = pullOf(
      node({
        merged: true,
        state: "MERGED",
        autoMergeRequest: null,
        commits: {
          nodes: [
            {
              commit: {
                statusCheckRollup: {
                  contexts: {
                    nodes: [
                      { __typename: "CheckRun", name: "a", status: "QUEUED", conclusion: null },
                      {
                        __typename: "CheckRun",
                        name: "b",
                        status: "COMPLETED",
                        conclusion: "CANCELLED",
                        completedAt: "2026-11-27T00:00:00Z",
                      },
                      {
                        __typename: "CheckRun",
                        name: "c",
                        status: "COMPLETED",
                        conclusion: "SKIPPED",
                      },
                      {
                        __typename: "StatusContext",
                        context: "d",
                        state: "ERROR",
                        createdAt: "2026-11-27T00:00:00Z",
                      },
                      { __typename: "StatusContext", context: "e", state: "EXPECTED" },
                    ],
                  },
                },
              },
            },
          ],
        },
      }),
    );
    expect(pull).toMatchObject({ state: "merged", armed: false });
    expect(pull?.checks.map((check) => `${check.name}:${check.state}`)).toEqual([
      "a:pending",
      "b:failure",
      "c:success",
      "d:failure",
      "e:pending",
    ]);
  });

  it("puts the followed ids in the same request as the searches", () => {
    const document = buildRequest(searches, ["PR_1", "PR_2"]);
    expect(document).toContain("s0: search(");
    expect(document).toContain('followed: nodes(ids: ["PR_1","PR_2"])');
  });
});
