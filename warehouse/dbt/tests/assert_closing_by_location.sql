-- SPEC 5.3: closing headcount by location.
with actual as (
    select location, sum(is_active_eom) as n
    from {{ ref('fct_workforce_monthly') }}
    where month_start = {{ pl_time("'2025-12-01'") }}
    group by location
),
expected as (
    select 'San Jose' as location, 700 as n
    union all select 'New York', 420
    union all select 'Austin', 300
    union all select 'London', 220
    union all select 'Bengaluru', 140
)
select e.location, e.n as expected_n, a.n as actual_n
from expected as e
full outer join actual as a on a.location = e.location
where a.n is null or e.n is null or a.n <> e.n
