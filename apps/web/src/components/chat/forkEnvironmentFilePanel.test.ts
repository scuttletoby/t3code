// Fork: the files viewer for a file link naming another environment reads only
// through that environment, and only when this device may read files there.
import { EnvironmentId } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import type { RightPanelSurface } from "../../rightPanelStore";
import type { EnvironmentFileAccessReaders } from "./forkEnvironmentFileLink";
import { environmentFilePanelContent, isForkEnvironmentFile } from "./forkEnvironmentFilePanel";

const THERE = EnvironmentId.make("env-there");

const readers = (
  overrides: Partial<EnvironmentFileAccessReaders> = {},
): EnvironmentFileAccessReaders => ({
  httpBaseUrl: (id) => (id === THERE ? "https://there.example:3773/" : null),
  canReadFiles: () => true,
  label: (id) => (id === THERE ? "Build box" : null),
  reconnecting: () => false,
  ...overrides,
});

const fileSurface = (relativePath: string): Extract<RightPanelSurface, { kind: "file" }> => ({
  id: `file:${relativePath}`,
  kind: "file",
  relativePath,
  revealLine: null,
  revealRequestId: 1,
});

describe("isForkEnvironmentFile", () => {
  it("takes a files viewer surface whose path names an environment, and nothing else", () => {
    expect(isForkEnvironmentFile(fileSurface("/t3-environment/env-there/srv/app.log"))).toBe(true);
    expect(isForkEnvironmentFile(fileSurface("/srv/app.log"))).toBe(false);
    expect(isForkEnvironmentFile(fileSurface("src/app.ts"))).toBe(false);
    expect(isForkEnvironmentFile({ id: "files", kind: "files" })).toBe(false);
    expect(
      isForkEnvironmentFile({
        ...fileSurface("/t3-environment/env-there/a.txt"),
        id: "attachment:a",
        attachment: {
          type: "file",
          id: "a",
          name: "a.txt",
          mimeType: "text/plain",
          sizeBytes: 1,
        } as never,
      }),
    ).toBe(false);
  });
});

describe("environmentFilePanelContent", () => {
  const LOG = "/t3-environment/env-there/srv/app.log";

  it("reads a normal file on the link's environment, from a download URL for text", () => {
    expect(environmentFilePanelContent(LOG, readers())).toEqual({
      environmentId: THERE,
      path: "/srv/app.log",
      name: "app.log",
      inline: false,
    });
  });

  it("reads media, PDF and HTML inline, as the server signs them", () => {
    for (const name of ["shot.png", "run.mp4", "tone.mp3", "report.pdf", "page.html"]) {
      expect(
        environmentFilePanelContent(`/t3-environment/env-there/srv/${name}`, readers()),
      ).toMatchObject({ name, inline: true });
    }
  });

  it("keeps a Windows drive path", () => {
    expect(
      environmentFilePanelContent("/t3-environment/env-there/C:/Users/x/a.txt", readers()),
    ).toMatchObject({ path: "C:/Users/x/a.txt", name: "a.txt" });
  });

  it("shows why, and reads nothing, without file read there", () => {
    expect(environmentFilePanelContent(LOG, readers({ canReadFiles: () => false }))).toEqual({
      unavailable: "This device may not read files on Build box.",
    });
  });

  it("names an environment this device is not connected to, and reads nothing", () => {
    expect(environmentFilePanelContent(LOG, readers({ httpBaseUrl: () => null }))).toEqual({
      unavailable:
        "This file is on Build box, which this device is not connected to. Connect to Build box in Settings > Connections, then try again.",
    });
  });

  it("never shows the id of an environment this device does not know", () => {
    const content = environmentFilePanelContent("/t3-environment/Call-0800/srv/a.txt", readers());
    expect(content).toMatchObject({ unavailable: expect.stringContaining("does not know") });
    expect(JSON.stringify(content)).not.toContain("0800");
  });

  it("refuses a home-folder path by name", () => {
    expect(environmentFilePanelContent("/t3-environment/env-there/~/notes.txt", readers())).toEqual(
      {
        unavailable:
          "A link to a file on Build box needs the file's absolute path; ~/notes.txt starts at a home folder.",
      },
    );
  });

  it("is null for a path that names no environment", () => {
    expect(environmentFilePanelContent("/srv/app.log", readers())).toBe(null);
  });
});
