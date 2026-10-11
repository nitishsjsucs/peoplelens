-- Bootstrap phase A, step 2: datasets (idempotent). Rendered by `pl-ops bootstrap`
-- with {project}. CREATE SCHEMA IF NOT EXISTS creates; ALTER SCHEMA converges the options.
-- Time travel: 48 hours on raw, 168 (the 7-day maximum) on semantic; the 7-day
-- fail-safe after that window is recoverable only through Google support.
CREATE SCHEMA IF NOT EXISTS `{project}`.peoplelens_raw OPTIONS (location = "US", max_time_travel_hours = 48, description = "PeopleLens raw synthetic seeds (PII columns policy-tagged in phase C)");
CREATE SCHEMA IF NOT EXISTS `{project}`.peoplelens_staging OPTIONS (location = "US", max_time_travel_hours = 168, description = "PeopleLens staging views (no PII)");
CREATE SCHEMA IF NOT EXISTS `{project}`.peoplelens_core OPTIONS (location = "US", max_time_travel_hours = 168, description = "PeopleLens core tables");
CREATE SCHEMA IF NOT EXISTS `{project}`.peoplelens_semantic OPTIONS (location = "US", max_time_travel_hours = 168, description = "PeopleLens semantic layer: authorized views read by the Worker");
ALTER SCHEMA `{project}`.peoplelens_raw SET OPTIONS (max_time_travel_hours = 48);
ALTER SCHEMA `{project}`.peoplelens_staging SET OPTIONS (max_time_travel_hours = 168);
ALTER SCHEMA `{project}`.peoplelens_core SET OPTIONS (max_time_travel_hours = 168);
ALTER SCHEMA `{project}`.peoplelens_semantic SET OPTIONS (max_time_travel_hours = 168);
