// Locally generated RS256 keys and Access-shaped JWTs for tests.
import { type JWK, type JWTPayload, SignJWT, exportJWK, generateKeyPair } from "jose";

export const TEST_AUD = "peoplelens-test-aud";
export const TEST_ISSUER = "https://peoplelens-local.cloudflareaccess.test";

export interface TestKeys {
  kid: string;
  privateKey: CryptoKey;
  publicJwk: JWK;
  jwksJson: string;
}

export async function makeKeys(kid = "test-key-1"): Promise<TestKeys> {
  const { privateKey, publicKey } = await generateKeyPair("RS256", { extractable: true });
  const publicJwk = { ...(await exportJWK(publicKey)), kid, alg: "RS256", use: "sig" };
  return { kid, privateKey, publicJwk, jwksJson: JSON.stringify({ keys: [publicJwk] }) };
}

export interface MintOptions {
  aud?: string | string[];
  iss?: string;
  expiresIn?: string | number;
  kid?: string;
  claims?: JWTPayload;
}

export async function mintToken(keys: TestKeys, opts: MintOptions = {}): Promise<string> {
  const jwt = new SignJWT({ type: "app", ...opts.claims })
    .setProtectedHeader({ alg: "RS256", kid: opts.kid ?? keys.kid })
    .setIssuedAt()
    .setAudience(opts.aud ?? [TEST_AUD])
    .setIssuer(opts.iss ?? TEST_ISSUER);
  if (typeof opts.expiresIn === "number") jwt.setExpirationTime(opts.expiresIn);
  else jwt.setExpirationTime(opts.expiresIn ?? "10m");
  return jwt.sign(keys.privateKey);
}

export function userClaims(email: string): JWTPayload {
  return { email, sub: `user-${email}`, type: "app" };
}

export function serviceClaims(clientId: string): JWTPayload {
  return { common_name: clientId, sub: "", type: "app" };
}

export const LOCAL_BINDINGS = {
  "email:analyst@example.test": { role: "people_analytics", scope: { departments: "all" } },
  "email:hrbp@example.test": { role: "hr_business_partner", scope: { departments: ["Engineering", "Sales"] } },
  "email:recruiter@example.test": { role: "talent_acquisition", scope: { departments: "all" } },
  "email:exec@example.test": { role: "executive", scope: { departments: "all" } },
  "svc:0123456789abcdef0123456789abcdef.access": { role: "executive", scope: { departments: "all" } },
} as const;

export const ROLE_EMAILS = {
  people_analytics: "analyst@example.test",
  hr_business_partner: "hrbp@example.test",
  talent_acquisition: "recruiter@example.test",
  executive: "exec@example.test",
} as const;
