-- SPEC 5.3: closing headcount by job level.
with actual as (
    select job_level, sum(is_active_eom) as n
    from {{ ref('fct_workforce_monthly') }}
    where month_start = {{ pl_time("'2025-12-01'") }}
    group by job_level
),
expected as (
    select 'L1' as job_level, 300 as n
    union all select 'L2', 520
    union all select 'L3', 480
    union all select 'L4', 290
    union all select 'L5', 140
    union all select 'L6', 50
)
select e.job_level, e.n as expected_n, a.n as actual_n
from expected as e
full outer join actual as a on a.job_level = e.job_level
where a.n is null or e.n is null or a.n <> e.n
