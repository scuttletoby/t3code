// Fork: what a chat file link's download says when it fails (#17).
import { AssetWorkspaceAssetNotFoundError, ThreadId } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import { forkMarkdownDownloadError } from "./forkMarkdownFileDownload";

const resource = {
  _tag: "media-file" as const,
  threadId: ThreadId.make("thread-1"),
  path: "/etc/example/service.token",
  disposition: "attachment" as const,
};

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

describe("forkMarkdownDownloadError", () => {
  it("names the machine and the path the server checked for a missing file", () => {
    const error = forkMarkdownDownloadError(
      new AssetWorkspaceAssetNotFoundError({ resource, checkedPath: "/srv/notes/plan.txt" }),
      { requestedPath: "~/notes/plan.txt", machine: "Build server" },
    );

    expect(messageOf(error)).toBe("The file was not found on Build server at /srv/notes/plan.txt.");
    expect(messageOf(error)).not.toContain("Media file");
  });

  it("falls back to the requested path when an older server sends no checked path", () => {
    const error = forkMarkdownDownloadError(new AssetWorkspaceAssetNotFoundError({ resource }), {
      requestedPath: "/etc/example/service.token",
      machine: "Build server",
    });

    expect(messageOf(error)).toBe(
      "The file was not found on Build server at /etc/example/service.token.",
    );
  });

  it("leaves out the machine when the viewer runs on the server's own machine", () => {
    const error = forkMarkdownDownloadError(new AssetWorkspaceAssetNotFoundError({ resource }), {
      requestedPath: "/etc/example/service.token",
      machine: null,
    });

    expect(messageOf(error)).toBe("The file was not found at /etc/example/service.token.");
  });

  it("reports any other failure as it came", () => {
    const cause = new Error("Reconnect to the environment and try again.");

    expect(
      forkMarkdownDownloadError(cause, { requestedPath: "/tmp/a.txt", machine: "Build server" }),
    ).toBe(cause);
  });
});
