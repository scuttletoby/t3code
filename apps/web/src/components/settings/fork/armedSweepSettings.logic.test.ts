/**
 * Fork (issue 880): the armed PR sweep panel's choices. Questions: does each select offer exactly
 * the setting's values, and does a number field never save a value the server refuses?
 */
import { ArmedSweepMode, ArmedSweepScope } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  ARMED_SWEEP_MODE_OPTIONS,
  ARMED_SWEEP_SCOPE_OPTIONS,
  boundCount,
  labelOf,
  MIN_INTERVAL_SECONDS,
} from "./armedSweepSettings.logic";

describe("armed sweep settings", () => {
  it("offers exactly the setting's modes and scopes", () => {
    expect(ARMED_SWEEP_MODE_OPTIONS.map((option) => option.value)).toEqual([
      ...ArmedSweepMode.literals,
    ]);
    expect(ARMED_SWEEP_SCOPE_OPTIONS.map((option) => option.value)).toEqual([
      ...ArmedSweepScope.literals,
    ]);
    expect(labelOf(ARMED_SWEEP_MODE_OPTIONS, "dry")).toBe("Dry run");
  });

  it("bounds a number to a whole count, the interval to its floor", () => {
    expect(boundCount(-3)).toBe(0);
    expect(boundCount(2.6)).toBe(3);
    expect(boundCount(Number.NaN)).toBe(0);
    expect(boundCount(10, MIN_INTERVAL_SECONDS)).toBe(60);
    expect(boundCount(300, MIN_INTERVAL_SECONDS)).toBe(300);
  });
});
