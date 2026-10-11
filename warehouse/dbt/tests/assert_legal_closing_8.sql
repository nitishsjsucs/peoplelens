-- SPEC 5.3: Legal has 8 people at close, deliberately below the cohort threshold of 10.
select sum(is_active_eom) as n
from {{ ref('sem_workforce_monthly') }}
where month_start = {{ pl_time("'2025-12-01'") }}
  and department = 'Legal'
having sum(is_active_eom) <> 8
