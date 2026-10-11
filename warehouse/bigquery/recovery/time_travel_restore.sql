-- Restore a table as it was at a point inside the dataset's time-travel window
-- (48 h on peoplelens_raw, 168 h elsewhere) into a NEW table; never overwrite in place.
-- Render {project}, {dataset}, {table} and {timestamp} (UTC, e.g. 2026-10-10 12:00:00+00).
CREATE TABLE `{project}`.{dataset}.{table}_restored_{suffix}
AS SELECT * FROM `{project}`.{dataset}.{table}
FOR SYSTEM_TIME AS OF TIMESTAMP '{timestamp}';
