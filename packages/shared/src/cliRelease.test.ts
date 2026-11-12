import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import {
  cliArchiveTarCommand,
  newestCliReleaseVersion,
  parseChecksums,
  resolveCliReleaseRepository,
} from "./cliRelease.ts";

describe("cliRelease", () => {
  it("parses sha256sum output including binary-mode markers", () => {
    const checksums = parseChecksums(
      [
        `${"a".repeat(64)}  t3-1.2.3-linux-x64.tar.gz`,
        `${"B".repeat(64)} *t3-1.2.3-win32-x64.zip`,
        "not a checksum line",
        "",
      ].join("\n"),
    );
    expect(checksums.get("t3-1.2.3-linux-x64.tar.gz")).toBe("a".repeat(64));
    expect(checksums.get("t3-1.2.3-win32-x64.zip")).toBe("b".repeat(64));
    expect(checksums.size).toBe(2);
  });

  it("extracts with the System32 bsdtar on Windows and plain tar elsewhere", () => {
    expect(cliArchiveTarCommand("linux", {})).toBe("tar");
    expect(cliArchiveTarCommand("win32", { SystemRoot: "D:\\Win" })).toBe(
      "D:\\Win\\System32\\tar.exe",
    );
    expect(cliArchiveTarCommand("win32", {})).toBe("C:\\Windows\\System32\\tar.exe");
  });

  it("picks the newest non-draft release on the requested channel", () => {
    const releases = [
      { tag_name: "v1.2.4-preview.20260912.9", draft: true },
      { tag_name: "v1.2.4-preview.20260912.8" },
      { tag_name: "v1.2.4-nightly.20260912.7" },
      { tag_name: "desktop-preview" },
      { tag_name: "v1.2.3" },
      { tag_name: "v1.2.3-nightly.20260911.2" },
    ];
    expect(newestCliReleaseVersion(releases, "preview")).toBe("1.2.4-preview.20260912.8");
    expect(newestCliReleaseVersion(releases, "nightly")).toBe("1.2.4-nightly.20260912.7");
    expect(newestCliReleaseVersion(releases, "stable")).toBe("1.2.3");
    expect(newestCliReleaseVersion([{ tag_name: "v1.2.3" }], "preview")).toBeUndefined();
  });
});

describe("cliRelease repository", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("defaults to upstream when the build names no repository", () => {
    expect(resolveCliReleaseRepository(undefined)).toBe("pingdotgg/t3code");
    expect(resolveCliReleaseRepository("")).toBe("pingdotgg/t3code");
    expect(resolveCliReleaseRepository("  ")).toBe("pingdotgg/t3code");
  });

  it("lists and downloads releases only from the repository the build names", async () => {
    vi.stubGlobal("__T3CODE_RELEASE_REPOSITORY__", "fork-owner/t3code");
    vi.resetModules();
    const release = await import("./cliRelease.ts");
    expect(release.cliReleaseIndexPageUrl(2)).toBe(
      "https://api.github.com/repos/fork-owner/t3code/releases?per_page=100&page=2",
    );
    expect(release.cliReleaseDownloadBaseUrl("0.1.9-fork.1")).toBe(
      "https://github.com/fork-owner/t3code/releases/download/v0.1.9-fork.1",
    );
    expect(release.cliReleaseAssetUrl("0.1.9-fork.1", "SHA256SUMS")).toBe(
      "https://github.com/fork-owner/t3code/releases/download/v0.1.9-fork.1/SHA256SUMS",
    );
    expect(release.CLI_RELEASES_PAGE_URL).toBe("https://github.com/fork-owner/t3code/releases");
  });

  it("keeps upstream URLs in a build without the setting", async () => {
    vi.resetModules();
    const release = await import("./cliRelease.ts");
    expect(release.cliReleaseIndexPageUrl(1)).toBe(
      "https://api.github.com/repos/pingdotgg/t3code/releases?per_page=100&page=1",
    );
    expect(release.cliReleaseDownloadBaseUrl("0.1.9")).toBe(
      "https://github.com/pingdotgg/t3code/releases/download/v0.1.9",
    );
  });
});
