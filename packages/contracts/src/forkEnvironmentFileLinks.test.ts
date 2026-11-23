// Fork: file links that name the environment holding the file.
import { describe, expect, it } from "vite-plus/test";

import { EnvironmentId } from "./baseSchemas.ts";
import {
  ENVIRONMENT_FILE_LINK_AGENT_HINT,
  parseEnvironmentFileLinkPath,
} from "./forkEnvironmentFileLinks.ts";

const THERE = EnvironmentId.make("env-there");

describe("parseEnvironmentFileLinkPath", () => {
  it.each([
    ["/t3-environment/env-there/var/log/app.log", "/var/log/app.log"],
    ["/t3-environment/env-there//var/log/app.log", "/var/log/app.log"],
    ["/t3-environment/env-there///server/share/a.txt", "/server/share/a.txt"],
    ["/t3-environment/env-there/C:/Users/a/notes.txt", "C:/Users/a/notes.txt"],
    ["/t3-environment/env-there/srv/My File.md", "/srv/My File.md"],
  ])("%s names %s on its environment", (filePath, path) => {
    expect(parseEnvironmentFileLinkPath(filePath)).toEqual({ environmentId: THERE, path });
  });

  it.each([
    "/var/log/app.log",
    "t3-environment/env-there/var/log/app.log",
    "/T3-Environment/env-there/var/log/app.log",
    "/t3-environment/env-there",
    "/t3-environment/env-there/",
    "/t3-environment//var/log/app.log",
  ])("%s is not an environment file link", (filePath) => {
    expect(parseEnvironmentFileLinkPath(filePath)).toBeNull();
  });

  it("the agent hint's example parses once filled in", () => {
    const example = ENVIRONMENT_FILE_LINK_AGENT_HINT.match(/\]\(([^)]+)\)/)?.[1];
    expect(example).toBe("/t3-environment/<environmentId>/<absolute path>");
    const filled = example!
      .replace("<environmentId>", "env-there")
      .replace("<absolute path>", "srv/app.log");
    expect(parseEnvironmentFileLinkPath(filled)).toEqual({
      environmentId: THERE,
      path: "/srv/app.log",
    });
  });
});
