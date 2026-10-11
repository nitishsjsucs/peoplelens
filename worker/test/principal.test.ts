import { describe, expect, it } from "vitest";
import type { AccessClaims } from "../src/auth/access";
import { bindingKey, parseRoleBindings, resolvePrincipal } from "../src/auth/principal";
import { ApiError } from "../src/policy/types";
import { LOCAL_BINDINGS } from "./helpers/keys";

const bindings = parseRoleBindings(JSON.stringify(LOCAL_BINDINGS));
const base = { aud: ["x"], iss: "https://t.cloudflareaccess.com", exp: 9999999999 };
const SVC = "0123456789abcdef0123456789abcdef.access";

function user(email: string, extra: Partial<AccessClaims> = {}): AccessClaims {
  return { ...base, sub: "user-id-1", type: "app", email, ...extra };
}
function svc(commonName: string, extra: Partial<AccessClaims> = {}): AccessClaims {
  return { ...base, sub: "", type: "app", common_name: commonName, ...extra };
}
function err(fn: () => unknown): ApiError {
  try {
    fn();
  } catch (e) {
    if (e instanceof ApiError) return e;
    throw e;
  }
  throw new Error("expected an ApiError");
}

describe("resolvePrincipal", () => {
  it("binds the four local users to the four roles", () => {
    expect(resolvePrincipal(user("analyst@example.test"), bindings).role).toBe("people_analytics");
    expect(resolvePrincipal(user("recruiter@example.test"), bindings).role).toBe("talent_acquisition");
    expect(resolvePrincipal(user("exec@example.test"), bindings).role).toBe("executive");
    const hrbp = resolvePrincipal(user("hrbp@example.test"), bindings);
    expect(hrbp.role).toBe("hr_business_partner");
    expect(hrbp.scope.departments).toEqual(["Engineering", "Sales"]);
    expect(hrbp.kind).toBe("user");
  });

  it("lowercases emails before lookup", () => {
    expect(resolvePrincipal(user("Analyst@Example.TEST"), bindings).subject).toBe("email:analyst@example.test");
  });

  it("binds service tokens only in the svc namespace", () => {
    const p = resolvePrincipal(svc(SVC), bindings);
    expect(p).toMatchObject({ subject: `svc:${SVC}`, kind: "service_token", role: "executive" });
    // A user whose email happens to look like a client id is still a user.
    expect(err(() => resolvePrincipal(user(SVC), bindings)).code).toBe("NO_ROLE_BINDING");
    // A service token cannot claim a user binding through common_name.
    expect(err(() => resolvePrincipal(svc("analyst@example.test"), bindings)).code).toBe("NO_ROLE_BINDING");
  });

  it("requires type app and an identity", () => {
    expect(err(() => resolvePrincipal(user("analyst@example.test", { type: "org" }), bindings)).status).toBe(401);
    expect(err(() => resolvePrincipal({ ...base, sub: "", type: "app" }, bindings)).code).toBe("UNAUTHENTICATED");
    // empty sub with an email is neither a user token nor a service token
    expect(err(() => resolvePrincipal(user("analyst@example.test", { sub: "" }), bindings)).code).toBe("UNAUTHENTICATED");
    expect(bindingKey(svc(SVC)).kind).toBe("service_token");
  });

  it("denies unknown identities by default", () => {
    const e = err(() => resolvePrincipal(user("stranger@example.test"), bindings));
    expect(e.status).toBe(403);
    expect(e.code).toBe("NO_ROLE_BINDING");
  });
});

describe("parseRoleBindings", () => {
  it("rejects malformed configuration", () => {
    const bad = [
      undefined,
      "not json",
      "[]",
      JSON.stringify({ "Email:Upper@example.test": { role: "executive", scope: { departments: "all" } } }),
      JSON.stringify({ "email:a@example.test": { role: "admin", scope: { departments: "all" } } }),
      JSON.stringify({ "email:a@example.test": { role: "executive", scope: { departments: ["Nope"] } } }),
      JSON.stringify({ "email:a@example.test": { role: "executive", scope: { departments: [] } } }),
      JSON.stringify({ "email:a@example.test": { role: "hr_business_partner", scope: { departments: "all" } } }),
      JSON.stringify({ "svc:not-a-client-id": { role: "executive", scope: { departments: "all" } } }),
    ];
    for (const json of bad) expect(err(() => parseRoleBindings(json)).code).toBe("CONFIG_INVALID");
  });
});
