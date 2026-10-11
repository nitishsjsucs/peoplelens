import { describe, expect, it } from "vitest";
import { grainHint, rangeFor, resolveTimePhrase } from "../src/planner/timephrases";

function r(text: string) {
  const h = resolveTimePhrase(text);
  return h ? [h.start, h.end] : null;
}

describe("resolveTimePhrase (as of 2025-12-31)", () => {
  it("years and explicit ranges", () => {
    expect(r("turnover in 2024")).toEqual(["2024-01-01", "2024-12-31"]);
    expect(r("headcount 2023 vs 2025")).toEqual(["2023-01-01", "2025-12-31"]);
    expect(r("from 2023 to 2024")).toEqual(["2023-01-01", "2024-12-31"]);
    expect(r("since 2024")).toEqual(["2024-01-01", "2025-12-31"]);
  });

  it("quarters and halves", () => {
    expect(r("Q3 2025")).toEqual(["2025-07-01", "2025-09-30"]);
    expect(r("the second quarter of 2024")).toEqual(["2024-04-01", "2024-06-30"]);
    expect(r("2024 Q1")).toEqual(["2024-01-01", "2024-03-31"]);
    expect(r("H2 2023")).toEqual(["2023-07-01", "2023-12-31"]);
    expect(r("first half of 2025")).toEqual(["2025-01-01", "2025-06-30"]);
    expect(r("from Q2 2024 to Q1 2025")).toEqual(["2024-04-01", "2025-03-31"]);
  });

  it("months and points in time", () => {
    expect(r("in March 2024")).toEqual(["2024-03-01", "2024-03-31"]);
    expect(r("Feb 2024")).toEqual(["2024-02-01", "2024-02-29"]);
    const end = resolveTimePhrase("headcount at the end of 2024");
    expect(end).toMatchObject({ start: "2024-12-01", end: "2024-12-31", point: true });
  });

  it("relative phrases", () => {
    expect(r("last year")).toEqual(["2024-01-01", "2024-12-31"]);
    expect(r("this year")).toEqual(["2025-01-01", "2025-12-31"]);
    expect(r("YTD")).toEqual(["2025-01-01", "2025-12-31"]);
    expect(r("last quarter")).toEqual(["2025-07-01", "2025-09-30"]);
    expect(r("this quarter")).toEqual(["2025-10-01", "2025-12-31"]);
    expect(r("last month")).toEqual(["2025-11-01", "2025-11-30"]);
    expect(r("over the past 12 months")).toEqual(["2025-01-01", "2025-12-31"]);
    expect(r("in the last two years")).toEqual(["2024-01-01", "2025-12-31"]);
    expect(r("over the last three years")).toEqual(["2023-01-01", "2025-12-31"]);
    expect(resolveTimePhrase("how many people right now")).toMatchObject({ start: "2025-12-01", end: "2025-12-31", point: true });
    expect(r("headcount by department")).toBeNull();
  });

  it("a point on a flow metric widens to its year", () => {
    expect(rangeFor("flow", resolveTimePhrase("turnover right now"))).toEqual({ start: "2025-01-01", end: "2025-12-31" });
    expect(rangeFor("stock", resolveTimePhrase("headcount right now"))).toEqual({ start: "2025-12-01", end: "2025-12-31" });
    expect(rangeFor("stock", null)).toEqual({ start: "2025-12-01", end: "2025-12-31" });
    expect(rangeFor("flow", null)).toEqual({ start: "2025-01-01", end: "2025-12-31" });
  });

  it("grain words", () => {
    expect(grainHint("monthly headcount")).toBe("month");
    expect(grainHint("turnover by quarter")).toBe("quarter");
    expect(grainHint("year over year")).toBe("year");
    expect(grainHint("headcount in 2024")).toBeNull();
  });
});
