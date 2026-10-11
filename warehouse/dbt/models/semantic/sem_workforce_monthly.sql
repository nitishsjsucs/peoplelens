{%- set bq_config = pl_bq_config(authorized_view_of='core', layer='semantic') -%}
{%- if bq_config %}{{ config(**bq_config) }}{% endif %}
-- Semantic-layer view (BigQuery: authorized view over peoplelens_core).
-- No employee id: workforce_key is a hash, used only as the entity key.
select
    workforce_key,
    month_start,
    department,
    location,
    job_level,
    employment_type,
    tenure_band,
    is_active_eom,
    fte_eom,
    is_termination,
    is_voluntary_termination,
    base_salary_eom
from {{ ref('fct_workforce_monthly') }}
