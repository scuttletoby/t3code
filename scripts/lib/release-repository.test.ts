import { describe, expect, it } from "vite-plus/test";

import { releaseRepositoryDefine, resolveReleaseRepository } from "./release-repository.ts";

describe("release repository build setting", () => {
  it("is empty (the upstream default) when unset or blank", () => {
    expect(resolveReleaseRepository({})).toBe("");
    expect(resolveReleaseRepository({ T3CODE_RELEASE_REPOSITORY: "  " })).toBe("");
  });

  it("keeps a valid owner/repo", () => {
    expect(resolveReleaseRepository({ T3CODE_RELEASE_REPOSITORY: " some-owner/t3code " })).toBe(
      "some-owner/t3code",
    );
    expect(releaseRepositoryDefine({ T3CODE_RELEASE_REPOSITORY: "some-owner/t3code" })).toEqual({
      __T3CODE_RELEASE_REPOSITORY__: JSON.stringify("some-owner/t3code"),
    });
  });

  it("names the repository a GitHub Actions build runs in, unless the setting overrides it", () => {
    const actions = { GITHUB_ACTIONS: "true", GITHUB_REPOSITORY: "fork-owner/t3code" };
    expect(resolveReleaseRepository(actions)).toBe("fork-owner/t3code");
    expect(
      resolveReleaseRepository({ ...actions, T3CODE_RELEASE_REPOSITORY: "other/t3code" }),
    ).toBe("other/t3code");
    // Outside Actions a stray GITHUB_REPOSITORY changes nothing.
    expect(resolveReleaseRepository({ GITHUB_REPOSITORY: "fork-owner/t3code" })).toBe("");
  });

  it("fails the build on a malformed value instead of falling back to upstream", () => {
    for (const value of ["owner", "owner/repo/extra", "owner/..", "https://github.com/o/r"]) {
      expect(() => resolveReleaseRepository({ T3CODE_RELEASE_REPOSITORY: value })).toThrow(
        /T3CODE_RELEASE_REPOSITORY/,
      );
    }
  });
});
