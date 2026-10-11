// Cloudflare Access JWT verification. Production: RS256 against the team's
// remote JWKS (https://<team>.cloudflareaccess.com/cdn-cgi/access/certs, keys
// rotate every 6 weeks, so never pin one). Local: a static JWKS minted by
// `pl dev keys`. The verification code is identical; only the key source differs.
import {
  type JSONWebKeySet,
  type JWTVerifyGetKey,
  createLocalJWKSet,
  createRemoteJWKSet,
  errors as joseErrors,
  jwtVerify,
} from "jose";
import { ApiError } from "../policy/types";

export const ACCESS_HEADER = "Cf-Access-Jwt-Assertion";

export type JWKSource = { kind: "static"; jwks: JSONWebKeySet } | { kind: "remote"; url: string };

export interface AccessClaims {
  aud: string[];
  iss: string;
  sub: string;
  exp: number;
  iat?: number;
  type?: string;
  email?: string;
  common_name?: string;
}

const remoteSets = new Map<string, JWTVerifyGetKey>();

function keyResolver(source: JWKSource): JWTVerifyGetKey {
  if (source.kind === "static") return createLocalJWKSet(source.jwks);
  let set = remoteSets.get(source.url);
  if (!set) {
    set = createRemoteJWKSet(new URL(source.url), { cooldownDuration: 30_000, cacheMaxAge: 600_000 });
    remoteSets.set(source.url, set);
  }
  return set;
}

export function teamCertsUrl(teamDomain: string): string {
  return `https://${teamDomain}/cdn-cgi/access/certs`;
}

export function teamIssuer(teamDomain: string): string {
  return `https://${teamDomain}`;
}

function unauthenticated(message: string): ApiError {
  return new ApiError(401, "UNAUTHENTICATED", message);
}

/** Verify signature (RS256, kid from the JWKS), aud (array contains ours), iss and exp. */
export async function verifyAccessJwt(
  token: string | null | undefined,
  cfg: { aud: string; issuer: string; jwks: JWKSource },
): Promise<AccessClaims> {
  if (!token) throw unauthenticated(`missing ${ACCESS_HEADER} header`);
  try {
    const { payload } = await jwtVerify(token, keyResolver(cfg.jwks), {
      algorithms: ["RS256"],
      audience: cfg.aud,
      issuer: cfg.issuer,
      requiredClaims: ["exp", "aud", "iss"],
    });
    const aud = Array.isArray(payload.aud) ? payload.aud : payload.aud ? [payload.aud] : [];
    const claims: AccessClaims = {
      aud,
      iss: String(payload.iss),
      sub: typeof payload.sub === "string" ? payload.sub : "",
      exp: Number(payload.exp),
    };
    if (typeof payload.iat === "number") claims.iat = payload.iat;
    if (typeof payload["type"] === "string") claims.type = payload["type"];
    if (typeof payload["email"] === "string") claims.email = payload["email"];
    if (typeof payload["common_name"] === "string") claims.common_name = payload["common_name"];
    return claims;
  } catch (err) {
    if (err instanceof joseErrors.JWTExpired) throw unauthenticated("token expired");
    if (err instanceof joseErrors.JWTClaimValidationFailed) throw unauthenticated(`claim check failed: ${err.claim}`);
    if (err instanceof joseErrors.JOSEError) throw unauthenticated("invalid token");
    throw unauthenticated("invalid token");
  }
}
