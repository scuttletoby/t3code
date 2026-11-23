// Fork: file links that name the environment holding the file.
import { describe, expect, it } from "vite-plus/test";

import { EnvironmentId } from "./baseSchemas.ts";
import {
  ENVIRONMENT_FILE_LINK_AGENT_HINT,
  isAbsoluteEnvironmentFilePath,
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
    "/t3-environment/env\\x/var/log/app.log",
    "/t3-environment/env%2fx/var/log/app.log",
    "/t3-environment/env%5cx/var/log/app.log",
    "/t3-environment/env there/var/log/app.log",
  ])("%s is not an environment file link", (filePath) => {
    expect(parseEnvironmentFileLinkPath(filePath)).toBeNull();
  });

  it.each([
    ["/t3-environment/env-there/~/notes.txt", "~/notes.txt"],
    ["/t3-environment/env-there//~/notes.txt", "~/notes.txt"],
    ["/t3-environment/env-there/~", "~"],
  ])("%s keeps its home-folder path %s, which is not absolute", (filePath, path) => {
    expect(parseEnvironmentFileLinkPath(filePath)).toEqual({ environmentId: THERE, path });
    expect(isAbsoluteEnvironmentFilePath(path)).toBe(false);
  });

  it("absolute paths on either system are absolute", () => {
    expect(isAbsoluteEnvironmentFilePath("/var/log/app.log")).toBe(true);
    expect(isAbsoluteEnvironmentFilePath("C:/Users/a/notes.txt")).toBe(true);
    expect(isAbsoluteEnvironmentFilePath("C:\\Users\\a\\notes.txt")).toBe(true);
  });

  it("the agent hint asks for an absolute path, never ~", () => {
    expect(ENVIRONMENT_FILE_LINK_AGENT_HINT).toContain("must be absolute");
    expect(ENVIRONMENT_FILE_LINK_AGENT_HINT).toContain("never `~`");
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
