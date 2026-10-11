{%- set bq_config = pl_bq_config(authorized_view_of='core', layer='semantic') -%}
{%- if bq_config %}{{ config(**bq_config) }}{% endif %}
select
    offer_id,
    department,
    location,
    job_level,
    offer_date,
    is_accepted
from {{ ref('fct_offers') }}
