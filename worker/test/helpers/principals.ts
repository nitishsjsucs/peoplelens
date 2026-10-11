import type { Principal, RoleId } from "../../src/policy/types";

export function principal(role: RoleId, departments: string[] | "all" = "all"): Principal {
  return {
    subject: `email:${role}@example.test`,
    kind: "user",
    role,
    scope: { departments: role === "hr_business_partner" && departments === "all" ? ["Engineering", "Sales"] : departments },
  };
}

export const ANALYST = principal("people_analytics");
export const HRBP = principal("hr_business_partner", ["Engineering", "Sales"]);
export const RECRUITER = principal("talent_acquisition");
export const EXEC = principal("executive");
