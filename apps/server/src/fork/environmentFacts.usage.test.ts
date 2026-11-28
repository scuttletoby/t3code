/**
 * Fork (fleet usage, fork issue 1146): a linked machine's `usage` rides on the same answer as its
 * placement facts. Placement never reads it: a usage part it cannot decode never refuses the
 * machine, and an answer with only `usage` is still "no facts" for placement.
 */
import { describe, expect, it } from "@effect/vitest";

import { factsFromAnswer } from "./environmentFacts.ts";

const placement = { labels: [], available: true, slotsFree: 2, factsAt: 5 };

describe("factsFromAnswer and usage", () => {
  it("decodes placement beside a usage part it cannot read", () => {
    const facts = factsFromAnswer({ placement, usage: { readAt: "not a number" } }, 10);
    expect(facts).toEqual({ placement, receivedAt: 10 });
  });

  it("is no facts for an answer with only usage", () => {
    expect(factsFromAnswer({ usage: { readAt: 1, accounts: [] } }, 10)).toBeNull();
  });
});
