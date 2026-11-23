// Fork: file links that name the environment holding the file.
import {
  AssetWorkspaceAssetNotFoundError,
  EnvironmentId,
  ThreadId,
  type AssetResource,
} from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import { AsyncResult } from "effect/reactivity";
import { describe, expect, it, vi } from "vite-plus/test";

import { parseEnvironmentFileLinkHref } from "@t3tools/shared/forkEnvironmentFileLinks";

import { resolveMarkdownFileLinkMeta } from "../../markdown-links";
import {
  environmentFileAccess,
  environmentFileDownloadUrl,
  environmentFileLinkTarget,
  type EnvironmentFileAccessReaders,
} from "./forkEnvironmentFileLink";

const HERE = EnvironmentId.make("env-here");
const THERE = EnvironmentId.make("env-there");
const THREAD = ThreadId.make("thread-1");

const readers = (
  overrides: Partial<EnvironmentFileAccessReaders> = {},
): EnvironmentFileAccessReaders => ({
  httpBaseUrl: (id) => (id === THERE ? "https://there.example:3773/" : null),
  canReadFiles: () => true,
  label: (id) => (id === THERE ? "Build box" : null),
  ...overrides,
});

describe("parseEnvironmentFileLinkHref", () => {
  it.each([
    ["t3-file://v1/env-there/home/a/app.log", "/home/a/app.log"],
    ["t3-file://v1/env-there//home/a/app.log:12", "/home/a/app.log:12"],
    ["t3-file://v1/env-there/C:/Users/a/notes.txt", "/C:/Users/a/notes.txt"],
    ["T3-FILE://v1/env-there/srv/My%20File.md#L4", "/srv/My%20File.md#L4"],
  ])("%s names %s on its environment", (href, pathHref) => {
    expect(parseEnvironmentFileLinkHref(href)).toEqual({ environmentId: THERE, pathHref });
  });

  it.each([
    "/home/a/app.log",
    "file:///home/a/app.log",
    "t3-thread://v2/env-there/thread-1",
    "t3-file://v1/env-there",
    "t3-file://v1/env-there/",
    "t3-file://v1//home/a/app.log",
    "t3-file://v2/env-there/home/a/app.log",
  ])("%s is not an environment file link", (href) => {
    expect(parseEnvironmentFileLinkHref(href)).toBeNull();
  });

  it("never turns a doubled slash into a network path", () => {
    const link = parseEnvironmentFileLinkHref("t3-file://v1/env-there///server/share/a.txt");
    expect(link?.pathHref).toBe("/server/share/a.txt");
    expect(resolveMarkdownFileLinkMeta(link!.pathHref)?.filePath).toBe("/server/share/a.txt");
  });

  it("resolves to the same file link meta as the plain path", () => {
    const link = parseEnvironmentFileLinkHref("t3-file://v1/env-there/home/a/src/app.ts:42");
    expect(resolveMarkdownFileLinkMeta(link!.pathHref)).toEqual(
      resolveMarkdownFileLinkMeta("/home/a/src/app.ts:42"),
    );
  });
});

describe("environmentFileLinkTarget", () => {
  it("opens a file on the message's own environment like a path link", () => {
    expect(environmentFileLinkTarget("t3-file://v1/env-here/home/a/x.ts", HERE)).toEqual({
      kind: "local",
      pathHref: "/home/a/x.ts",
    });
  });

  it("keeps a file on another environment there", () => {
    expect(environmentFileLinkTarget("t3-file://v1/env-there/home/a/x.ts", HERE)).toEqual({
      kind: "remote",
      link: { environmentId: THERE, pathHref: "/home/a/x.ts" },
    });
    expect(environmentFileLinkTarget("t3-file://v1/env-there/home/a/x.ts", null)?.kind).toBe(
      "remote",
    );
  });

  it("leaves links without an environment alone", () => {
    expect(environmentFileLinkTarget("/home/a/x.ts", HERE)).toBeNull();
  });
});

describe("environmentFileAccess", () => {
  it("uses the named environment's own connection", () => {
    expect(environmentFileAccess(THERE, readers())).toEqual({
      httpBaseUrl: "https://there.example:3773/",
    });
  });

  it("names an environment this device is not connected to", () => {
    expect(environmentFileAccess(THERE, readers({ httpBaseUrl: () => null }))).toEqual({
      unavailable:
        "This file is on Build box, which this device is not connected to. Connect to Build box in Settings > Connections, then try again.",
    });
    const unknown = EnvironmentId.make("env-unknown");
    expect(environmentFileAccess(unknown, readers())).toEqual({
      unavailable:
        "This file is on env-unknown, which this device is not connected to. Connect to env-unknown in Settings > Connections, then try again.",
    });
  });

  it("refuses without file read there", () => {
    expect(environmentFileAccess(THERE, readers({ canReadFiles: () => false }))).toEqual({
      unavailable: "This device may not read files on Build box.",
    });
  });
});

type CreateAssetUrl = Parameters<typeof environmentFileDownloadUrl>[0]["createAssetUrl"];

describe("environmentFileDownloadUrl", () => {
  const download = (
    createAssetUrl: CreateAssetUrl,
    overrides: Partial<EnvironmentFileAccessReaders> = {},
  ) =>
    environmentFileDownloadUrl({
      environmentId: THERE,
      path: "/home/a/app.log",
      threadId: THREAD,
      readers: readers(overrides),
      createAssetUrl,
    });

  it("asks the file's environment for a download URL and resolves it against that connection", async () => {
    const createAssetUrl = vi.fn<CreateAssetUrl>(async () =>
      AsyncResult.success({
        relativeUrl: "/api/assets/token123/app.log",
        expiresAt: 0,
      }),
    );
    await expect(download(createAssetUrl)).resolves.toBe(
      "https://there.example:3773/api/assets/token123/app.log",
    );
    const resource: AssetResource = {
      _tag: "media-file",
      threadId: THREAD,
      path: "/home/a/app.log",
      disposition: "attachment",
    };
    expect(createAssetUrl).toHaveBeenCalledWith({ environmentId: THERE, input: { resource } });
  });

  it("asks nothing when the environment is not connected", async () => {
    const createAssetUrl = vi.fn<CreateAssetUrl>();
    await expect(download(createAssetUrl, { httpBaseUrl: () => null })).rejects.toThrow(
      "This file is on Build box, which this device is not connected to.",
    );
    expect(createAssetUrl).not.toHaveBeenCalled();
  });

  it("says where it looked when the file is missing there", async () => {
    const createAssetUrl = vi.fn<CreateAssetUrl>(async () =>
      AsyncResult.failure(
        Cause.fail(
          new AssetWorkspaceAssetNotFoundError({
            resource: { _tag: "media-file", threadId: THREAD, path: "/home/a/app.log" },
            checkedPath: "/home/a/app.log",
          }),
        ),
      ),
    );
    await expect(download(createAssetUrl)).rejects.toThrow(
      "The file was not found on Build box at /home/a/app.log.",
    );
  });
});
