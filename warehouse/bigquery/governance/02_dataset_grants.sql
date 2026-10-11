-- Bootstrap phase A, step 4: dataset-level read grants (idempotent DCL). No write role anywhere.
-- The Worker and the eval service account read only the semantic dataset (authorized
-- views over core); the analyst account (masking demo) reads core.
GRANT `roles/bigquery.dataViewer` ON SCHEMA `{project}`.peoplelens_semantic
  TO "serviceAccount:peoplelens-worker@{project}.iam.gserviceaccount.com",
     "serviceAccount:peoplelens-eval@{project}.iam.gserviceaccount.com";
GRANT `roles/bigquery.dataViewer` ON SCHEMA `{project}`.peoplelens_core
  TO "serviceAccount:peoplelens-analyst@{project}.iam.gserviceaccount.com";
