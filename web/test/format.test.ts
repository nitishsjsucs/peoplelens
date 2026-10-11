import { describe, expect, it } from "vitest";
import { ApiClient, headers, isLoopback } from "../src/api";
import { formatCell, formatMeasure, formatPeriod, hiddenCount } from "../src/format";

describe("formatting", () => {
  it("formats measures by unit and never shows a suppressed value", () => {
    expect(formatMeasure("employees", 620)).toBe("620");
    expect(formatMeasure("ratio", 0.1234)).toBe("12.3%");
    expect(formatMeasure("USD", 125000.4)).toBe("$125,000");
    expect(formatMeasure("FTE", 1724)).toBe("1,724.0");
    expect(formatMeasure("days", 61.27)).toBe("61.3 d");
    expect(formatMeasure("employees", null)).toBe("hidden");
    const col = { name: "headcount", kind: "measure" as const, unit: "employees" };
    expect(formatCell(col, { headcount: 8, suppressed: true })).toBe("hidden");
  });

  it("labels periods by grain", () => {
    expect(formatPeriod("2024-04-01", "quarter")).toBe("Q2 2024");
    expect(formatPeriod("2024-04-01", "year")).toBe("2024");
    expect(formatPeriod("2024-04-01", "month")).toBe("Apr 2024");
    expect(hiddenCount({ primary: 1, complementary: 1, hidden_sum: 0, complement: 2 })).toBe(4);
  });
});

describe("api client", () => {
  it("sends the local token only when one is set", async () => {
    expect(headers(null)).toEqual({ "content-type": "application/json" });
    expect(headers("t")["Cf-Access-Jwt-Assertion"]).toBe("t");
    expect(isLoopback("127.0.0.1") && isLoopback("localhost") && !isLoopback("peoplelens.example.workers.dev")).toBe(true);
    const seen: { url: string; init: RequestInit | undefined }[] = [];
    const client = new ApiClient("tok", async (url, init) => {
      seen.push({ url: String(url), init });
      return Response.json({ session_id: "s1" });
    });
    expect(await client.session()).toBe("s1");
    expect(seen[0]?.url).toBe("/api/sessions");
    expect((seen[0]?.init?.headers as Record<string, string>)["Cf-Access-Jwt-Assertion"]).toBe("tok");
  });
});
