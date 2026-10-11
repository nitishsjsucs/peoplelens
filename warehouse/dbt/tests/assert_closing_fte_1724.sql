-- SPEC 5.3: closing FTE 1,724.0 = 1,620 + 40 + 64.
select sum(case when is_active_eom = 1 then fte_eom else 0 end) as fte
from {{ ref('fct_workforce_monthly') }}
where month_start = {{ pl_time("'2025-12-01'") }}
having abs(sum(case when is_active_eom = 1 then fte_eom else 0 end) - 1724.0) > 0.000001
