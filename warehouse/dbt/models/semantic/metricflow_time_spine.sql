{{ config(materialized='table') }}
{%- set bq_config = pl_bq_config(layer='semantic') -%}
{%- if bq_config %}{{ config(**bq_config) }}{% endif %}
-- Day-grain time spine, 2019-01-01 to 2026-12-31, declared with the time_spine YAML config.
select
    {{ pl_time('date_day') }} as date_day
from (
    {{ pl_day_series('2019-01-01', '2026-12-31') }}
) as days
