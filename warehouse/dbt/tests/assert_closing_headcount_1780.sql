-- SPEC 5.3: active on 2025-12-31, measured on the mart at the last month end.
select sum(is_active_eom) as n
from {{ ref('fct_workforce_monthly') }}
where month_start = {{ pl_time("'2025-12-01'") }}
having sum(is_active_eom) <> 1780
