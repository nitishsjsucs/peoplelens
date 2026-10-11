-- Bootstrap phase C, after the tagged dbt build (idempotent DCL). The masking-demo account
-- reads peoplelens_core only once the policy tags and data masking policies exist: it is a
-- masked reader of every tag and a fine-grained reader of none, so it sees masked salaries.
-- Granted in phase A it could read raw base_salary_eom until phase C ran (and again after any
-- rebuild that dropped the tags), so it lives here.
GRANT `roles/bigquery.dataViewer` ON SCHEMA `{project}`.peoplelens_core
  TO "serviceAccount:peoplelens-analyst@{project}.iam.gserviceaccount.com";
