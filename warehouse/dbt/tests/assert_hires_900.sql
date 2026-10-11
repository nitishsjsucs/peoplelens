-- SPEC 5.3: hires in window 900 (2023: 280, 2024: 300, 2025: 320).
with actual as (
    select {{ dbt.date_trunc('year', 'hire_date') }} as yr, count(*) as n
    from {{ ref('dim_employee') }}
    where hire_date >= cast('2023-01-01' as date) and hire_date <= cast('2025-12-31' as date)
    group by 1
),
expected as (
    select cast('2023-01-01' as date) as yr, 280 as n
    union all select cast('2024-01-01' as date), 300
    union all select cast('2025-01-01' as date), 320
)
select e.yr, e.n as expected_n, a.n as actual_n
from expected as e
left join actual as a on cast(a.yr as date) = e.yr
where a.n is null or a.n <> e.n
union all
select cast(null as date), 900, sum(n) from actual having sum(n) <> 900
