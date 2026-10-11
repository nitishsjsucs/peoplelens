// Map verified Access claims to a principal through ROLE_BINDINGS_JSON.
// Users bind as `email:<lowercased email>`; Access service tokens bind as
// `svc:<common_name>` (the client id, `<hex>.access`) only when the token has
// type "app" and an empty sub. The two namespaces never mix. No binding: 403.
import { POLICY } from "../policy/policy";
import { ApiError, type Policy, type Principal, type Scope, isRoleId } from "../policy/types";
import type { AccessClaims } from "./access";

export interface RoleBinding {
  role: string;
  scope: { departments: string[] | "all" };
}
export type RoleBindings = Record<string, RoleBinding>;

export function parseRoleBindings(json: string | undefined, policy: Policy = POLICY): RoleBindings {
  if (!json) throw new ApiError(503, "CONFIG_INVALID", "ROLE_BINDINGS_JSON is not set");
  let raw: unknown;
  try {
    raw = JSON.parse(json);
  } catch {
    throw new ApiError(503, "CONFIG_INVALID", "ROLE_BINDINGS_JSON is not valid JSON");
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new ApiError(503, "CONFIG_INVALID", "ROLE_BINDINGS_JSON must be an object");
  }
  const out: RoleBindings = {};
  const departments = policy.dimensions.department.values;
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const v = value as RoleBinding;
    const keyOk =
      (key.startsWith("email:") && key === key.toLowerCase() && key.length > 6) ||
      /^svc:[0-9a-f]+\.access$/.test(key);
    if (!keyOk) throw new ApiError(503, "CONFIG_INVALID", `role binding key ${JSON.stringify(key)} is malformed`);
    if (!v || !isRoleId(v.role)) throw new ApiError(503, "CONFIG_INVALID", `role binding ${key}: unknown role`);
    const d = v.scope?.departments;
    const scopeOk =
      d === "all" ||
      (Array.isArray(d) && d.length > 0 && d.every((x) => typeof x === "string" && departments.includes(x)));
    if (!scopeOk) throw new ApiError(503, "CONFIG_INVALID", `role binding ${key}: invalid scope`);
    if (policy.roles[v.role].scope === "assigned_departments" && d === "all") {
      throw new ApiError(503, "CONFIG_INVALID", `role binding ${key}: ${v.role} needs assigned departments`);
    }
    out[key] = { role: v.role, scope: { departments: d === "all" ? "all" : [...d].sort() } };
  }
  return out;
}

export function bindingKey(claims: AccessClaims): { key: string; kind: Principal["kind"] } {
  if (claims.type !== "app") {
    throw new ApiError(401, "UNAUTHENTICATED", "not an Access application token");
  }
  if (claims.sub === "" && claims.common_name && claims.email === undefined) {
    return { key: `svc:${claims.common_name}`, kind: "service_token" };
  }
  if (claims.sub !== "" && claims.email) {
    return { key: `email:${claims.email.toLowerCase()}`, kind: "user" };
  }
  throw new ApiError(401, "UNAUTHENTICATED", "token carries neither a user email nor a service token identity");
}

export function resolvePrincipal(claims: AccessClaims, bindings: RoleBindings): Principal {
  const { key, kind } = bindingKey(claims);
  const binding = bindings[key];
  if (!binding || !isRoleId(binding.role)) {
    throw new ApiError(403, "NO_ROLE_BINDING", "no role is bound to this identity");
  }
  const scope: Scope = { departments: binding.scope.departments };
  return { subject: key, kind, role: binding.role, scope };
}
