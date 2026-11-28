// @effect-diagnostics nodeBuiltinImport:off - makes real workspace folders, as the server reads them.
import * as NodeFSP from "node:fs/promises";
import * as NodeOS from "node:os";
import * as NodePath from "node:path";

import { describe, expect, it } from "@effect/vitest";
import {
  IssueTrackerKind,
  type OrchestrationProjectShell,
  type ProjectId,
  SourceControlProviderKind,
} from "@t3tools/contracts";
import type { IssueTrackerApi } from "@t3tools/source-control-core/server/IssueTracker";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import { afterAll } from "vite-plus/test";

import * as IssueService from "../issue/IssueService.ts";
import * as IssueTrackerRegistry from "../issue/IssueTrackerRegistry.ts";
import * as IssueTrackerBindings from "../persistence/IssueTrackerBindings.ts";
import * as ProjectService from "../project/ProjectService.ts";
import * as RepositoryIdentityResolver from "../project/RepositoryIdentityResolver.ts";
import { applyIssuesConfig, readIssuesConfig } from "./issueRepos.ts";

const roots: string[] = [];
afterAll(() =>
  Promise.all(roots.map((root) => NodeFSP.rm(root, { recursive: true, force: true }))),
);

/** A workspace root holding `.t3code/issues.json` with `text`, or no file when `text` is null. */
const workspace = (text: string | null) =>
  Effect.promise(async () => {
    const root = await NodeFSP.mkdtemp(NodePath.join(NodeOS.tmpdir(), "fork-issue-repos-"));
    roots.push(root);
    if (text !== null) {
      await NodeFSP.mkdir(NodePath.join(root, ".t3code"));
      await NodeFSP.writeFile(NodePath.join(root, ".t3code", "issues.json"), text);
    }
    return root;
  });

const UPSTREAM = { tracker: "github", host: "github.com", scope: "acme/web" };
const OWN = { tracker: "github", host: "github.com", scope: "me/web" };
const LINEAR = { tracker: "linear", host: "linear.app", scope: "ENG" };
const STANDALONE = new Set(["linear"]);

// Which repositories does a project list for each state of its file?
describe("applyIssuesConfig", () => {
  const guessed = [LINEAR, UPSTREAM, OWN];

  it("keeps upstream's guess without a file, or a file without repositories", () => {
    expect(applyIssuesConfig(guessed, STANDALONE, { kind: "absent" })).toEqual(guessed);
    expect(
      applyIssuesConfig(guessed, STANDALONE, { kind: "named", repositories: undefined }),
    ).toEqual(guessed);
  });

  it("lists only the named repositories, so a fork's upstream drops out", () => {
    expect(
      applyIssuesConfig(guessed, STANDALONE, { kind: "named", repositories: ["Me/Web"] }),
    ).toEqual([LINEAR, OWN]);
  });

  it("serves a named repository git did not guess on the guessed host", () => {
    expect(
      applyIssuesConfig(guessed, STANDALONE, {
        kind: "named",
        repositories: ["me/web", "me/docs", "me/docs"],
      }),
    ).toEqual([LINEAR, OWN, { ...UPSTREAM, scope: "me/docs" }]);
  });

  it("lists no git-host repository for an empty list, or with nothing guessed to serve it", () => {
    expect(applyIssuesConfig(guessed, STANDALONE, { kind: "named", repositories: [] })).toEqual([
      LINEAR,
    ]);
    expect(
      applyIssuesConfig([LINEAR], STANDALONE, { kind: "named", repositories: ["me/web"] }),
    ).toEqual([LINEAR]);
  });

  it("fails closed on an unreadable file: no git-host repository, the standalone tracker kept", () => {
    expect(applyIssuesConfig(guessed, STANDALONE, { kind: "unreadable", detail: "x" })).toEqual([
      LINEAR,
    ]);
  });
});

// Does the server read the file as a person writes it, and tell a missing file from a broken one?
describe("readIssuesConfig", () => {
  it.effect("reads the named repositories", () =>
    Effect.gen(function* () {
      const root = yield* workspace('{ "repositories": ["me/web"] }');
      expect(yield* readIssuesConfig(root)).toEqual({ kind: "named", repositories: ["me/web"] });
    }),
  );

  it.effect("reads a missing file, or a missing folder, as absent", () =>
    Effect.gen(function* () {
      expect(yield* readIssuesConfig(yield* workspace(null))).toEqual({ kind: "absent" });
      expect(yield* readIssuesConfig("/nonexistent-fork-issue-repos")).toEqual({
        kind: "absent",
      });
    }),
  );

  it.effect("ignores keys it does not know, so a newer file still reads", () =>
    Effect.gen(function* () {
      const root = yield* workspace('{ "repositories": [], "later": true }');
      expect(yield* readIssuesConfig(root)).toEqual({ kind: "named", repositories: [] });
    }),
  );

  it.effect("reads broken JSON, a bad name or a folder in the file's place as unreadable", () =>
    Effect.gen(function* () {
      for (const text of [
        "{ nope",
        '{ "repositories": ["me web"] }',
        '{ "repositories": "me/web" }',
      ]) {
        expect((yield* readIssuesConfig(yield* workspace(text))).kind, text).toBe("unreadable");
      }
      const root = yield* workspace(null);
      yield* Effect.promise(() =>
        NodeFSP.mkdir(NodePath.join(root, ".t3code", "issues.json"), { recursive: true }),
      );
      expect((yield* readIssuesConfig(root)).kind).toBe("unreadable");
    }),
  );
});

// Through the seam in IssueService.ts: does a fork checkout that names its own repository stop
// listing and writing to its upstream, while still reading an upstream link (fork issue 830)?
describe("IssueService with .t3code/issues.json", () => {
  const GITHUB = IssueTrackerKind.make("github");

  const forkProject = (workspaceRoot: string): OrchestrationProjectShell => ({
    id: "fork" as ProjectId,
    title: "fork",
    workspaceRoot,
    repositoryIdentity: {
      canonicalKey: "github.com/acme/web",
      locator: {
        source: "git-remote",
        remoteName: "upstream",
        remoteUrl: "https://github.com/acme/web.git",
      },
      provider: SourceControlProviderKind.make("github"),
      displayName: "acme/web",
      origin: { canonicalKey: "github.com/me/web" },
    },
    defaultModelSelection: null,
    scripts: [],
    createdAt: "2026-07-01T00:00:00Z",
    updatedAt: "2026-07-01T00:00:00Z",
  });

  const service = (projects: ReadonlyArray<OrchestrationProjectShell>, writes: string[]) => {
    const tracker = {
      kind: GITHUB,
      capabilities: {
        create: true,
        edit: true,
        comment: true,
        labels: true,
        assignees: true,
        reactions: false,
        templates: false,
        linkedPullRequests: true,
        events: true,
        search: true,
      },
      getViewer: () => Effect.succeed("me"),
      listIssues: (input) =>
        Effect.succeed({
          items: [
            {
              key: "1",
              displayKey: "#1",
              title: `Issue of ${input.scope}`,
              url: `https://github.com/${input.scope}/issues/1`,
              author: null,
              state: { category: "open", name: "open" },
              createdAt: "2026-07-01T00:00:00Z",
              updatedAt: "2026-07-02T00:00:00Z",
              closedAt: null,
              assignees: [],
              labels: [],
              commentCount: 0,
            },
          ],
          nextCursor: null,
        }),
      getIssue: () => Effect.die("not read"),
      getComments: () => Effect.succeed({ comments: [], nextCursor: null }),
      comment: (input) => Effect.sync(() => void writes.push(input.scope)),
      update: () => Effect.void,
      setState: () => Effect.void,
    } as IssueTrackerApi;
    return IssueService.IssueService.pipe(
      Effect.provide(
        IssueService.layer.pipe(
          Layer.provide(
            Layer.mergeAll(
              Layer.mock(IssueTrackerBindings.IssueTrackerBindingsRepository)({
                list: () => Effect.succeed(new Map()),
              }),
              Layer.succeed(
                IssueTrackerRegistry.IssueTrackerRegistry,
                IssueTrackerRegistry.fromTrackers([tracker]),
              ),
              Layer.mock(ProjectService.ProjectService)({
                listShells: () => Effect.succeed(projects),
                getShell: (projectId) =>
                  Effect.succeed(Option.fromNullishOr(projects.find((p) => p.id === projectId))),
              }),
              Layer.mock(RepositoryIdentityResolver.RepositoryIdentityResolver)({
                resolve: () => Effect.succeed(null),
              }),
            ),
          ),
        ),
      ),
    );
  };

  const comment = (scope: string) => ({
    projectId: "fork" as ProjectId,
    tracker: GITHUB,
    host: "github.com",
    scope,
    key: "1",
    body: "ok",
  });

  it.effect("lists and writes only the named repository", () =>
    Effect.gen(function* () {
      const writes: string[] = [];
      const root = yield* workspace('{ "repositories": ["me/web"] }');
      const issues = yield* service([forkProject(root)], writes);
      const listed = yield* issues.list({ state: "open" });
      expect(listed.entries.map((entry) => entry.scope)).toEqual(["me/web"]);
      expect(listed.scopes?.map((scope) => scope.scope)).toEqual(["me/web"]);
      yield* issues.comment(comment("me/web"));
      const refused = yield* Effect.flip(issues.comment(comment("acme/web")));
      expect(refused).toMatchObject({ _tag: "IssueOperationError", reason: "read-only" });
      expect(writes).toEqual(["me/web"]);
    }),
  );

  it.effect("keeps upstream's guess, upstream included, for a project without the file", () =>
    Effect.gen(function* () {
      const writes: string[] = [];
      const issues = yield* service([forkProject(yield* workspace(null))], writes);
      const listed = yield* issues.list({ state: "open" });
      expect(listed.entries.map((entry) => entry.scope).toSorted()).toEqual(["acme/web", "me/web"]);
      yield* issues.comment(comment("acme/web"));
      expect(writes).toEqual(["acme/web"]);
    }),
  );
});
