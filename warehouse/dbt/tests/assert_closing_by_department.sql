-- SPEC 5.3: closing headcount by department.
with actual as (
    select department, sum(is_active_eom) as n
    from {{ ref('fct_workforce_monthly') }}
    where month_start = {{ pl_time("'2025-12-01'") }}
    group by department
),
expected as (
    select 'Engineering' as department, 620 as n
    union all select 'Sales', 380
    union all select 'Customer Success', 250
    union all select 'Marketing', 150
    union all select 'Finance', 120
    union all select 'People', 90
    union all select 'Operations', 162
    union all select 'Legal', 8
)
select e.department, e.n as expected_n, a.n as actual_n
from expected as e
full outer join actual as a on a.department = e.department
where a.n is null or e.n is null or a.n <> e.n
