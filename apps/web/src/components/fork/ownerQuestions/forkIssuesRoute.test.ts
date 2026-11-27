import { describe, expect, it, vi } from "vite-plus/test";

// The pages themselves are not under test here, and they pull in workers a unit run cannot load.
vi.mock("../../issue/IssuesPage", () => ({ IssuesPage: () => null }));
vi.mock("./WaitingOnYouPage", () => ({ WaitingOnYouPage: () => null }));

import { Route } from "../../../routes/_chat.issues";
import { issuesSearchFromUrl } from "../../issue/issuesSearch.logic";
import { ForkIssuesPage } from "./ForkIssuesRoute";
import { issuesPageChoice } from "./forkIssuesRoute.logic";

// Does the Issues entry open the fork's page, keeping upstream's search handling (fork issue 828)?
describe("the Issues route", () => {
  it("renders the fork's page and still reads upstream's search", () => {
    expect(Route.options.component).toBe(ForkIssuesPage);
    expect(Route.options.validateSearch).toBe(issuesSearchFromUrl);
  });
});

// Which page does a device see for its setting, and before its settings are read?
describe("issuesPageChoice", () => {
  it("shows Waiting on you while the setting is on", () => {
    expect(issuesPageChoice("ready", true)).toBe("fork");
  });

  it("gives upstream's page back when the device turned it off", () => {
    expect(issuesPageChoice("ready", false)).toBe("upstream");
  });

  it("shows neither page until the device's settings are read", () => {
    expect(issuesPageChoice("pending", true)).toBe("none");
    expect(issuesPageChoice("pending", false)).toBe("none");
  });

  it("uses the default (on) when the settings could not be read", () => {
    expect(issuesPageChoice("failed", true)).toBe("fork");
    expect(issuesPageChoice("retrying", true)).toBe("fork");
  });
});
