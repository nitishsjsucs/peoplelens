{%- set bq_config = pl_bq_config(authorized_view_of='core', layer='semantic') -%}
{%- if bq_config %}{{ config(**bq_config) }}{% endif %}
-- Filled requisitions only: time to fill is measured on the filled date.
select
    requisition_id,
    department,
    location,
    job_level,
    filled_date,
    days_to_fill
from {{ ref('fct_requisitions') }}
where status = 'filled'
