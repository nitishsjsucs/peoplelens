"""Bootstrap phase C, steps 16 to 19: governance (not on the resume-critical path).

Taxonomy and policy tags (Data Catalog Policy Tag Manager), fine-grained
readers granted BEFORE tagging so nothing breaks, data masking policies, then a
dbt-bigquery build with apply_policy_tags=true that attaches the tags through
column-level persist_docs, and only then the masking-demo account's read grant on
peoplelens_core. The tag names are recorded in bootstrap-state.json, and every later
phase A seed and build passes the same vars (`policy_tag_vars`), because a rebuild
without them recreates the tables untagged. Doctor reports this group separately,
and requires the eight tagged columns once phase C has recorded its tags.
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


def policy_tag_vars(tag_names: dict[str, str] | None) -> dict | None:
    """The dbt vars that attach the policy tags, from the tag resource names phase C records
    in bootstrap-state.json (`policy_tags`, display name to resource name); None before
    phase C. Phase C's build and every later phase A seed and build pass them."""
    if not tag_names:
        return None
    spec = taxonomy_spec()
    missing = [t["displayName"] for t in spec["policy_tags"] if not tag_names.get(t["displayName"])]
    if missing:
        raise BootstrapError(f"bootstrap-state.json policy_tags lacks {', '.join(missing)}: rerun phase C")
    out: dict = {"apply_policy_tags": True}
    for tag in spec["policy_tags"]:
        out[tag["dbt_var"]] = tag_names[tag["displayName"]]
    return out


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
        plan.add("then grant dataViewer on peoplelens_core to peoplelens-analyst (03_analyst_core_grant.sql)")
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

    # 19. attach tags through dbt (column-level persist_docs); recorded first, so a phase A
    # rerun that follows keeps them even if this build is interrupted
    names = {t["displayName"]: tags[t["displayName"]]["name"] for t in spec["policy_tags"]}
    state = load_state()
    state["policy_tags"] = names
    save_state(state)
    dbt_vars = policy_tag_vars(names)
    env = dict(os.environ, GCP_PROJECT_ID=project)
    res = runner.run(["uv", "run", "pl", "dbt", "--dialect", "bigquery", "build", "--vars", json.dumps(dbt_vars, sort_keys=True)],
                     env=env, mutates=True)
    if not res.ok:
        raise BootstrapError(f"dbt build with policy tags failed: {res.stdout[-600:]}")
    plan.add("dbt build with apply_policy_tags=true: tags attached")

    # 19a. only now may the masking-demo account read peoplelens_core (masked)
    from peoplelens.ops.bootstrap_gcp import bq_script

    grant = runner.run(bq_script("03_analyst_core_grant.sql", project), mutates=True)
    if not grant.ok:
        raise BootstrapError(f"analyst grant on peoplelens_core failed: {(grant.stderr or grant.stdout).strip()[:300]}")
    plan.add("dataViewer on peoplelens_core for peoplelens-analyst (masked reader of every tag)")

    # 20. (below the cut) let the owner impersonate the analyst account for the masking demo
    from peoplelens.ops.bootstrap_gcp import sa_email

    res = runner.run(["gcloud", "iam", "service-accounts", "add-iam-policy-binding", sa_email("peoplelens-analyst", project),
                      f"--member=user:{owner_email}", "--role=roles/iam.serviceAccountTokenCreator",
                      f"--project={project}", "--quiet"], mutates=True)
    plan.add("token creator on peoplelens-analyst for the owner (masking demo): " + ("ok" if res.ok else "failed"))
