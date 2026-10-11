-- Bootstrap phase A, step 4: dataset-level read grants (idempotent DCL). No write role anywhere.
-- The Worker and the eval service account read only the semantic dataset (authorized
-- views over core). The analyst account (masking demo) is granted core in phase C, after
-- the policy tags are attached (03_analyst_core_grant.sql).
GRANT `roles/bigquery.dataViewer` ON SCHEMA `{project}`.peoplelens_semantic
  TO "serviceAccount:peoplelens-worker@{project}.iam.gserviceaccount.com",
     "serviceAccount:peoplelens-eval@{project}.iam.gserviceaccount.com";
