import { describe, expect, it } from "@effect/vitest";

import { usageReport } from "./usageReport.fixture.ts";
import { MESSAGES, parseUsageReport, versionIssue } from "./usageReport.ts";

describe("Antigravity /usage report", () => {
  it("maps each group's 5-hour and weekly buckets to usage windows", () => {
    expect(parseUsageReport(usageReport())).toEqual({
      _tag: "reading",
      windows: [
        {
          id: "gemini-weekly",
          label: "Gemini · Weekly",
          kind: "weekly",
          usedPercent: expect.closeTo(5, 6),
          resetsAt: "2026-12-04T20:59:40.000Z",
          windowDurationMins: 7 * 24 * 60,
        },
        {
          id: "gemini-5h",
          label: "Gemini · 5-hour",
          kind: "session",
          usedPercent: expect.closeTo(20, 6),
          resetsAt: "2026-11-28T01:59:40.000Z",
          windowDurationMins: 5 * 60,
        },
        {
          id: "3p-weekly",
          label: "Claude & GPT (shared) · Weekly",
          kind: "weekly",
          usedPercent: 100,
          resetsAt: "2026-12-02T17:21:33.000Z",
          windowDurationMins: 7 * 24 * 60,
        },
      ],
    });
  });

  it("gives a reason, never a 0 or 100 percent window, when the report is unusable", () => {
    expect(parseUsageReport("not json")).toEqual({ _tag: "failed", message: MESSAGES.unreadable });
    expect(parseUsageReport('{"status":')).toEqual({
      _tag: "failed",
      message: MESSAGES.unreadable,
    });
    expect(parseUsageReport(usageReport({ command: { name: "usage", data: {} } }))).toEqual({
      _tag: "failed",
      message: MESSAGES.unreadable,
    });
    expect(
      parseUsageReport(usageReport({ command: { name: "usage", data: { groups: [] } } })),
    ).toEqual({ _tag: "failed", message: MESSAGES.noLimits });
    expect(parseUsageReport("", "Please sign in with Google first.")).toEqual({
      _tag: "failed",
      message: MESSAGES.signedOut,
    });
    expect(
      parseUsageReport(usageReport({ status: "ERROR", command: undefined }), "Not authenticated"),
    ).toEqual({ _tag: "failed", message: MESSAGES.signedOut });
  });

  it("stops asking a CLI that answered /usage with a model turn", () => {
    for (const overrides of [{ num_turns: 1 }, { usage: { total_tokens: 12 } }]) {
      expect(parseUsageReport(usageReport(overrides))).toEqual({
        _tag: "failed",
        message: MESSAGES.spentTurn,
        stop: true,
      });
    }
  });

  it("runs only a CLI at or above the checked version", () => {
    expect(versionIssue("1.5.18")).toBeNull();
    expect(versionIssue("1.6.0")).toBeNull();
    expect(versionIssue("1.5.9")).toBe(MESSAGES.tooOld("1.5.9"));
    expect(versionIssue("1.1.11")).toBe(MESSAGES.tooOld("1.1.11"));
    expect(versionIssue(null)).toBe(MESSAGES.noVersion);
  });
});
