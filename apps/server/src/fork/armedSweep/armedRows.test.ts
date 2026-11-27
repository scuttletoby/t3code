/**
 * Fork (issue 880): `wait_list`'s armed rows. Questions: does scope `thread` list only the pull
 * requests that wake the caller and `project` every one of its project? Is nothing listed while
 * the sweep is off, and does a failed sweep say so?
 */
import { describe, expect, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";

import { armedRowsFor, publishArmedReport } from "./armedRows.ts";
import type { ArmedRow } from "./armedSweep.ts";

const PROJECT = ProjectId.make("project-1");
const ME = ThreadId.make("thread-me");
const OTHER = ThreadId.make("thread-other");

const row = (number: number, threadId: ThreadId | null, projectId: ProjectId | null): ArmedRow => ({
  pullRequest: `acme/app#${number}`,
  url: `https://github.com/acme/app/pull/${number}`,
  title: `Change ${number}`,
  head: "abcdef1",
  state: "red",
  detail: "failing: test",
  notGreenSince: 0,
  threadId,
  projectId,
  wake: "sent",
});

describe("armedRowsFor", () => {
  it("lists the caller's rows, or its project's", () => {
    publishArmedReport({
      mode: "on",
      sweptAt: 60_000,
      pullRequests: [
        row(1, ME, PROJECT),
        row(2, OTHER, PROJECT),
        row(3, null, ProjectId.make("p2")),
      ],
    });
    const mine = armedRowsFor({ threadId: ME, projectId: PROJECT });
    expect(mine?.pullRequests.map((entry) => entry.pullRequest)).toEqual(["acme/app#1"]);
    expect(mine).toMatchObject({ mode: "on", sweptAt: "1970-01-01T00:01:00.000Z" });
    expect(mine?.pullRequests[0]).toEqual(
      expect.objectContaining({ notGreenSince: "1970-01-01T00:00:00.000Z" }),
    );
    expect(mine?.pullRequests[0]).not.toHaveProperty("projectId");
    const project = armedRowsFor({ threadId: ME, projectId: PROJECT, scope: "project" });
    expect(project?.pullRequests.map((entry) => entry.pullRequest)).toEqual([
      "acme/app#1",
      "acme/app#2",
    ]);
  });

  it("says nothing while the sweep is off; a failed sweep's error is listed", () => {
    publishArmedReport(null);
    expect(armedRowsFor({ threadId: ME, projectId: PROJECT })).toBeUndefined();
    publishArmedReport({
      mode: "dry",
      sweptAt: 0,
      error: "the last sweep failed",
      pullRequests: [],
    });
    expect(armedRowsFor({ threadId: ME, projectId: PROJECT })).toMatchObject({
      mode: "dry",
      error: "the last sweep failed",
      pullRequests: [],
    });
  });
});
