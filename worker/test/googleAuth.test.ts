import { exportPKCS8, exportSPKI, generateKeyPair, importSPKI, jwtVerify } from "jose";
import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import {
  BIGQUERY_READONLY_SCOPE,
  GOOGLE_TOKEN_URL,
  GoogleTokenSource,
  clearTokenCache,
  parseServiceAccountKey,
} from "../src/warehouse/googleAuth";

let pem: string;
let spki: string;

beforeAll(async () => {
  const { privateKey, publicKey } = await generateKeyPair("RS256", { extractable: true });
  pem = await exportPKCS8(privateKey);
  spki = await exportSPKI(publicKey);
});
beforeEach(() => clearTokenCache());

function saJson() {
  return JSON.stringify({
    type: "service_account",
    client_email: "peoplelens-worker@peoplelens-abc123.iam.gserviceaccount.com",
    private_key: pem,
    private_key_id: "key-1",
  });
}

describe("Google service-account JWT bearer flow", () => {
  it("signs an RS256 assertion with the bigquery.readonly scope and a one-hour expiry", async () => {
    const requests: { url: string; body: string }[] = [];
    const fetcher: typeof fetch = async (input, init) => {
      requests.push({ url: String(input), body: String(init?.body) });
      return Response.json({ access_token: "ya29.token", expires_in: 3599 });
    };
    let now = 1_760_000_000_000;
    const src = new GoogleTokenSource(parseServiceAccountKey(saJson()), fetcher, () => now);
    expect(await src.token()).toBe("ya29.token");
    expect(requests).toHaveLength(1);
    expect(requests[0]?.url).toBe(GOOGLE_TOKEN_URL);
    const form = new URLSearchParams(requests[0]?.body);
    expect(form.get("grant_type")).toBe("urn:ietf:params:oauth:grant-type:jwt-bearer");
    const { payload, protectedHeader } = await jwtVerify(form.get("assertion") ?? "", await importSPKI(spki, "RS256"), {
      audience: GOOGLE_TOKEN_URL,
      issuer: "peoplelens-worker@peoplelens-abc123.iam.gserviceaccount.com",
      currentDate: new Date(now),
    });
    expect(protectedHeader).toMatchObject({ alg: "RS256", kid: "key-1" });
    expect(payload["scope"]).toBe(BIGQUERY_READONLY_SCOPE);
    expect((payload.exp ?? 0) - (payload.iat ?? 0)).toBeLessThanOrEqual(3600);

    // cached until 60 s before expiry
    now += 3_000_000;
    await src.token();
    expect(requests).toHaveLength(1);
    now += 560_000;
    await src.token();
    expect(requests).toHaveLength(2);
  });

  it("rejects keys without client_email or private_key", () => {
    expect(() => parseServiceAccountKey("{}")).toThrow();
    expect(() => parseServiceAccountKey("nope")).toThrow();
    expect(() => parseServiceAccountKey(undefined)).toThrow();
  });

  it("surfaces a failed token exchange as an error without the key", async () => {
    const src = new GoogleTokenSource(parseServiceAccountKey(saJson()), async () => new Response("denied", { status: 400 }));
    await expect(src.token()).rejects.toThrow(/token exchange failed/);
  });
});
