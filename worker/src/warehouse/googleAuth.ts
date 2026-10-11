// Google OAuth 2.0 JWT-bearer flow for a service account, inside a Worker.
// RS256 assertion signed with the SA private key (jose.importPKCS8); scope is
// bigquery.readonly; the access token is cached per isolate until 60 s before expiry.
import { SignJWT, importPKCS8 } from "jose";
import { ApiError } from "../policy/types";

export const BIGQUERY_READONLY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly";
export const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";

export interface ServiceAccountKey {
  client_email: string;
  private_key: string;
  private_key_id?: string;
}

export function parseServiceAccountKey(json: string | undefined): ServiceAccountKey {
  let raw: unknown;
  try {
    raw = JSON.parse(json ?? "");
  } catch {
    throw new ApiError(503, "CONFIG_INVALID", "GCP_SA_KEY_JSON is not valid JSON");
  }
  const k = raw as Partial<ServiceAccountKey> & { type?: string };
  if (!k || typeof k.client_email !== "string" || typeof k.private_key !== "string") {
    throw new ApiError(503, "CONFIG_INVALID", "GCP_SA_KEY_JSON lacks client_email or private_key");
  }
  const out: ServiceAccountKey = { client_email: k.client_email, private_key: k.private_key };
  if (typeof k.private_key_id === "string") out.private_key_id = k.private_key_id;
  return out;
}

interface CachedToken {
  token: string;
  expiresAtMs: number;
}

const cache = new Map<string, CachedToken>();
/** Token exchanges in flight, so parallel cold callers in one isolate share one fetch. */
const inflight = new Map<string, Promise<CachedToken>>();

export function clearTokenCache(): void {
  cache.clear();
  inflight.clear();
}

export class GoogleTokenSource {
  constructor(
    private readonly key: ServiceAccountKey,
    private readonly fetcher: typeof fetch = (input, init) => fetch(input, init),
    private readonly now: () => number = () => Date.now(),
  ) {}

  async assertion(): Promise<string> {
    const iat = Math.floor(this.now() / 1000);
    const pk = await importPKCS8(this.key.private_key, "RS256");
    const header: { alg: "RS256"; typ: "JWT"; kid?: string } = { alg: "RS256", typ: "JWT" };
    if (this.key.private_key_id) header.kid = this.key.private_key_id;
    return new SignJWT({ scope: BIGQUERY_READONLY_SCOPE })
      .setProtectedHeader(header)
      .setIssuer(this.key.client_email)
      .setAudience(GOOGLE_TOKEN_URL)
      .setIssuedAt(iat)
      .setExpirationTime(iat + 3600)
      .sign(pk);
  }

  async token(): Promise<string> {
    const email = this.key.client_email;
    const cached = cache.get(email);
    if (cached && cached.expiresAtMs - 60_000 > this.now()) return cached.token;
    let pending = inflight.get(email);
    if (!pending) {
      pending = this.exchange().finally(() => inflight.delete(email));
      inflight.set(email, pending);
    }
    return (await pending).token;
  }

  private async exchange(): Promise<CachedToken> {
    const body = new URLSearchParams({
      grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
      assertion: await this.assertion(),
    });
    const res = await this.fetcher(GOOGLE_TOKEN_URL, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    if (!res.ok) throw new ApiError(502, "INTERNAL", `Google token exchange failed (${res.status})`);
    const json = (await res.json()) as { access_token?: string; expires_in?: number };
    if (!json.access_token) throw new ApiError(502, "INTERNAL", "Google token exchange returned no token");
    const entry = { token: json.access_token, expiresAtMs: this.now() + (json.expires_in ?? 3600) * 1000 };
    cache.set(this.key.client_email, entry);
    return entry;
  }
}
