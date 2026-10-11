import { SignJWT } from "jose";
import { beforeAll, describe, expect, it } from "vitest";
import { verifyAccessJwt } from "../src/auth/access";
import { ApiError } from "../src/policy/types";
import { TEST_AUD, TEST_ISSUER, type TestKeys, makeKeys, mintToken, userClaims } from "./helpers/keys";

let keys: TestKeys;
let other: TestKeys;
const cfg = () => ({ aud: TEST_AUD, issuer: TEST_ISSUER, jwks: { kind: "static" as const, jwks: JSON.parse(keys.jwksJson) } });

async function expect401(p: Promise<unknown>, fragment?: string) {
  const err = await p.then(
    () => null,
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(ApiError);
  expect((err as ApiError).status).toBe(401);
  expect((err as ApiError).code).toBe("UNAUTHENTICATED");
  if (fragment) expect((err as ApiError).message).toContain(fragment);
}

beforeAll(async () => {
  keys = await makeKeys("k1");
  other = await makeKeys("k1");
});

describe("verifyAccessJwt", () => {
  it("accepts a valid RS256 token whose aud array contains ours", async () => {
    const token = await mintToken(keys, { aud: ["another-app", TEST_AUD], claims: userClaims("a@example.test") });
    const claims = await verifyAccessJwt(token, cfg());
    expect(claims.email).toBe("a@example.test");
    expect(claims.aud).toContain(TEST_AUD);
    expect(claims.type).toBe("app");
  });

  it("rejects a missing header", async () => {
    await expect401(verifyAccessJwt(undefined, cfg()), "missing");
    await expect401(verifyAccessJwt("", cfg()), "missing");
  });

  it("rejects a bad signature (same kid, different key)", async () => {
    const token = await mintToken(other, { claims: userClaims("a@example.test") });
    await expect401(verifyAccessJwt(token, cfg()));
  });

  it("rejects the wrong audience", async () => {
    const token = await mintToken(keys, { aud: ["someone-else"], claims: userClaims("a@example.test") });
    await expect401(verifyAccessJwt(token, cfg()), "aud");
  });

  it("rejects the wrong issuer", async () => {
    const token = await mintToken(keys, { iss: "https://evil.cloudflareaccess.com", claims: userClaims("a@example.test") });
    await expect401(verifyAccessJwt(token, cfg()), "iss");
  });

  it("rejects an expired token", async () => {
    const token = await mintToken(keys, { expiresIn: Math.floor(Date.now() / 1000) - 120, claims: userClaims("a@example.test") });
    await expect401(verifyAccessJwt(token, cfg()), "expired");
  });

  it("rejects an unknown kid", async () => {
    const token = await mintToken(keys, { kid: "not-in-jwks", claims: userClaims("a@example.test") });
    await expect401(verifyAccessJwt(token, cfg()));
  });

  it("rejects HS256 and unsigned tokens", async () => {
    const hs = await new SignJWT({ email: "a@example.test", type: "app" })
      .setProtectedHeader({ alg: "HS256", kid: "k1" })
      .setAudience(TEST_AUD)
      .setIssuer(TEST_ISSUER)
      .setExpirationTime("5m")
      .sign(new TextEncoder().encode("secret-secret-secret-secret-secret"));
    await expect401(verifyAccessJwt(hs, cfg()));
    const header = btoa(JSON.stringify({ alg: "none" })).replace(/=+$/, "");
    const body = btoa(JSON.stringify({ aud: TEST_AUD, iss: TEST_ISSUER, exp: 9999999999 })).replace(/=+$/, "");
    await expect401(verifyAccessJwt(`${header}.${body}.`, cfg()));
  });

  it("rejects garbage", async () => {
    await expect401(verifyAccessJwt("not.a.jwt", cfg()));
  });
});
