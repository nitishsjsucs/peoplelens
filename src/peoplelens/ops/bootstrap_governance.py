"""Bootstrap phase C, steps 16 to 19: governance (not on the resume-critical path).

Taxonomy and policy tags (Data Catalog Policy Tag Manager), fine-grained
readers granted BEFORE tagging so nothing breaks, data masking policies, then a
dbt-bigquery build with apply_policy_tags=true that attaches the tags through
column-level persist_docs. Doctor reports this group separately.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from peoplelens.ops.bootstrap_gcp import GOVERNANCE_DIR, BootstrapError
from peoplelens.ops.checks import Plan, Runner, load_state, save_state

DATACATALOG = "https://datacatalog.googleapis.com/v1"
DATAPOLICY = "https://bigquerydatapolicy.googleapis.com/v1"
FINE_GRAINED_READER = "roles/datacatalog.categoryFineGrainedReader"
MASKED_READER = "roles/bigquerydatapolicy.maskedReader"


class GoogleRest:
    def __init__(self, token: str, project: str):
        self.token = token
        self.project = project

    def call(self, method: str, url: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("authorization", f"Bearer {self.token}")
        req.add_header("x-goog-user-project", self.project)
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise BootstrapError(f"{method} {url.split('?')[0]} failed: HTTP {exc.code} {exc.read()[:300]!r}") from exc


def taxonomy_spec() -> dict:
    return json.loads((GOVERNANCE_DIR / "taxonomy.json").read_text())


def _members(template: list[str], project: str, owner: str) -> list[str]:
    return [m.replace("{project}", project).replace("{owner}", owner) for m in template]


def _add_binding(rest: GoogleRest, resource_url: str, role: str, members: list[str]) -> str:
    policy = rest.call("POST", f"{resource_url}:getIamPolicy", {})
    bindings = policy.get("bindings", [])
    binding = next((b for b in bindings if b.get("role") == role), None)
    if binding is None:
        binding = {"role": role, "members": []}
        bindings.append(binding)
    missing = [m for m in members if m not in binding["members"]]
    if not missing:
        return "unchanged"
    binding["members"].extend(missing)
    policy["bindings"] = bindings
    rest.call("POST", f"{resource_url}:setIamPolicy", {"policy": policy})
    return f"added {len(missing)}"


def phase_c(project: str, runner: Runner, plan: Plan) -> None:
    spec = taxonomy_spec()
    location = spec["location"]
    if runner.dry_run:
        plan.add(f"taxonomy {spec['taxonomy']['displayName']} in {location} with tags " + ", ".join(t["displayName"] for t in spec["policy_tags"]))
        plan.add("fine-grained readers before tagging; data policies SHA256, ALWAYS_NULL, DEFAULT_MASKING_VALUE; maskedReader for analyst")
        plan.add("pl dbt --dialect bigquery build --vars '{apply_policy_tags: true, ...}'")
        return
    token = runner.run(["gcloud", "auth", "print-access-token"])
    owner = runner.run(["gcloud", "config", "get-value", "account"])
    if not token.ok or not owner.ok or not owner.stdout.strip():
        raise BootstrapError("gcloud is not logged in (gcloud auth login)")
    rest = GoogleRest(token.stdout.strip(), project)
    owner_email = owner.stdout.strip()
    parent = f"projects/{project}/locations/{location}"

    # 16. taxonomy and tags
    taxonomies = rest.call("GET", f"{DATACATALOG}/{parent}/taxonomies").get("taxonomies", [])
    taxonomy = next((t for t in taxonomies if t.get("displayName") == spec["taxonomy"]["displayName"]), None)
    if taxonomy is None:
        taxonomy = rest.call("POST", f"{DATACATALOG}/{parent}/taxonomies", spec["taxonomy"])
        plan.add(f"created taxonomy {taxonomy['name']}")
    tags = {t["displayName"]: t for t in rest.call("GET", f"{DATACATALOG}/{taxonomy['name']}/policyTags").get("policyTags", [])}
    for tag in spec["policy_tags"]:
        if tag["displayName"] not in tags:
            tags[tag["displayName"]] = rest.call(
                "POST", f"{DATACATALOG}/{taxonomy['name']}/policyTags",
                {"displayName": tag["displayName"], "description": tag["description"]},
            )
            plan.add(f"created policy tag {tag['displayName']}")

    # 17. fine-grained readers first
    for tag_name, members in spec["fine_grained_readers"].items():
        result = _add_binding(rest, f"{DATACATALOG}/{tags[tag_name]['name']}", FINE_GRAINED_READER,
                              _members(members, project, owner_email))
        plan.add(f"fine-grained readers on {tag_name}: {result}")

    # 18. data masking policies
    existing = {p["dataPolicyId"]: p for p in rest.call("GET", f"{DATAPOLICY}/{parent}/dataPolicies").get("dataPolicies", [])}
    for tag in spec["policy_tags"]:
        policy_id = f"peoplelens_{tag['displayName']}"
        if policy_id not in existing:
            existing[policy_id] = rest.call("POST", f"{DATAPOLICY}/{parent}/dataPolicies", {
                "dataPolicyId": policy_id,
                "dataPolicyType": "DATA_MASKING_POLICY",
                "policyTag": tags[tag["displayName"]]["name"],
                "dataMaskingPolicy": {"predefinedExpression": tag["masking"]},
            })
            plan.add(f"created data policy {policy_id} ({tag['masking']})")
        result = _add_binding(rest, f"{DATAPOLICY}/{existing[policy_id]['name']}", MASKED_READER,
                              _members(spec["masked_readers"], project, owner_email))
        plan.add(f"masked readers on {policy_id}: {result}")

    # 19. attach tags through dbt (column-level persist_docs)
    dbt_vars = {"apply_policy_tags": True}
    for tag in spec["policy_tags"]:
        dbt_vars[tag["dbt_var"]] = tags[tag["displayName"]]["name"]
    env = dict(os.environ, GCP_PROJECT_ID=project)
    res = runner.run(["uv", "run", "pl", "dbt", "--dialect", "bigquery", "build", "--vars", json.dumps(dbt_vars)],
                     env=env, mutates=True)
    if not res.ok:
        raise BootstrapError(f"dbt build with policy tags failed: {res.stdout[-600:]}")
    plan.add("dbt build with apply_policy_tags=true: tags attached")
    state = load_state()
    state["policy_tags"] = {t["displayName"]: tags[t["displayName"]]["name"] for t in spec["policy_tags"]}
    save_state(state)
