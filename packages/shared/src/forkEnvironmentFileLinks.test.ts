// Fork: the link agents are told to write is the link the chat opens.
import { describe, expect, it } from "vite-plus/test";

import {
  ENVIRONMENT_FILE_LINK_AGENT_HINT,
  parseEnvironmentFileLinkHref,
} from "./forkEnvironmentFileLinks.ts";

describe("ENVIRONMENT_FILE_LINK_AGENT_HINT", () => {
  it("shows agents a link the chat reads back as that environment's file", () => {
    const example = /\]\((t3-file:\/\/[^)]+)\)/.exec(ENVIRONMENT_FILE_LINK_AGENT_HINT)?.[1];
    expect(example).toBe("t3-file://v1/<environmentId>/<absolute path>");
    const written = example!
      .replace("<environmentId>", "env-1")
      .replace("<absolute path>", "/srv/app.log");
    expect(parseEnvironmentFileLinkHref(written)).toEqual({
      environmentId: "env-1",
      pathHref: "/srv/app.log",
    });
  });
});
