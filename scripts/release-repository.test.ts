import { it as effectIt } from "@effect/vitest";
import * as ConfigProvider from "effect/ConfigProvider";
import * as Effect from "effect/Effect";
import { describe, expect, it } from "vite-plus/test";

import { resolveGitHubPublishConfig } from "../build-desktop-artifact.ts";
import { releaseRepositoryDefine, resolveReleaseRepository } from "./release-repository.ts";

const desktopFeedRepository = (env: Record<string, string>) =>
  resolveGitHubPublishConfig("latest").pipe(
    Effect.map((config) => (config ? `${config.owner}/${config.repo}` : "")),
    Effect.provide(ConfigProvider.layer(ConfigProvider.fromEnv({ env }))),
  );

describe("release repository build setting", () => {
  it("is empty (the upstream default) when unset or blank", () => {
    expect(resolveReleaseRepository({})).toBe("");
    expect(resolveReleaseRepository({ T3CODE_DESKTOP_UPDATE_REPOSITORY: "  " })).toBe("");
  });

  it("is upstream's desktop update setting, so the server follows the desktop feed", () => {
    const env = { T3CODE_DESKTOP_UPDATE_REPOSITORY: " some-owner/t3code " };
    expect(resolveReleaseRepository(env)).toBe("some-owner/t3code");
    expect(releaseRepositoryDefine(env)).toEqual({
      __T3CODE_RELEASE_REPOSITORY__: JSON.stringify("some-owner/t3code"),
    });
  });

  it("names the repository a GitHub Actions build runs in, unless the setting overrides it", () => {
    const actions = { GITHUB_ACTIONS: "true", GITHUB_REPOSITORY: "fork-owner/t3code" };
    expect(resolveReleaseRepository(actions)).toBe("fork-owner/t3code");
    expect(
      resolveReleaseRepository({ ...actions, T3CODE_DESKTOP_UPDATE_REPOSITORY: "other/t3code" }),
    ).toBe("other/t3code");
  });

  effectIt.effect("names the same repository as the desktop updater feed for every setting", () =>
    Effect.gen(function* () {
      const cases: Array<Record<string, string>> = [
        {},
        { T3CODE_DESKTOP_UPDATE_REPOSITORY: "fork-owner/t3code" },
        { GITHUB_REPOSITORY: "fork-owner/t3code" },
        { T3CODE_DESKTOP_UPDATE_REPOSITORY: "a/t3code", GITHUB_REPOSITORY: "b/t3code" },
        { T3CODE_RELEASE_REPOSITORY: "b/t3code", GITHUB_REPOSITORY: "b/t3code" },
      ];
      for (const env of cases) {
        const feed = yield* desktopFeedRepository(env);
        expect(resolveReleaseRepository(env), JSON.stringify(env)).toBe(feed);
      }
    }),
  );

  it("fails the build when the former setting disagrees with the desktop updater's", () => {
    for (const env of [
      { T3CODE_RELEASE_REPOSITORY: "fork-owner/t3code" },
      { T3CODE_RELEASE_REPOSITORY: "b/t3code", T3CODE_DESKTOP_UPDATE_REPOSITORY: "a/t3code" },
      { T3CODE_RELEASE_REPOSITORY: "b/t3code", GITHUB_REPOSITORY: "a/t3code" },
    ]) {
      expect(() => resolveReleaseRepository(env), JSON.stringify(env)).toThrow(
        /T3CODE_RELEASE_REPOSITORY=".+" disagrees.*Set T3CODE_DESKTOP_UPDATE_REPOSITORY/,
      );
    }
  });

  it("fails the build on a malformed value instead of falling back to upstream", () => {
    for (const value of ["owner", "owner/repo/extra", "owner/..", "https://github.com/o/r"]) {
      expect(() => resolveReleaseRepository({ T3CODE_DESKTOP_UPDATE_REPOSITORY: value })).toThrow(
        /T3CODE_DESKTOP_UPDATE_REPOSITORY/,
      );
    }
  });
});
