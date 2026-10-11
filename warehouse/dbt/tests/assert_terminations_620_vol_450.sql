-- SPEC 5.3: terminations in window 620 (2023: 190, 2024: 210, 2025: 220); voluntary 450, involuntary 170.
with t as (
    select * from {{ ref('dim_employee') }}
    where termination_date >= cast('2023-01-01' as date) and termination_date <= cast('2025-12-31' as date)
),
by_year as (
    select {{ dbt.date_trunc('year', 'termination_date') }} as yr, count(*) as n from t group by 1
),
expected as (
    select cast('2023-01-01' as date) as yr, 190 as n
    union all select cast('2024-01-01' as date), 210
    union all select cast('2025-01-01' as date), 220
)
select 'year' as check_name, e.n as expected_n, b.n as actual_n
from expected as e left join by_year as b on cast(b.yr as date) = e.yr
where b.n is null or b.n <> e.n
union all
select 'total', 620, count(*) from t having count(*) <> 620
union all
select 'voluntary', 450, sum(case when termination_type = 'voluntary' then 1 else 0 end) from t
having sum(case when termination_type = 'voluntary' then 1 else 0 end) <> 450
union all
select 'involuntary', 170, sum(case when termination_type = 'involuntary' then 1 else 0 end) from t
having sum(case when termination_type = 'involuntary' then 1 else 0 end) <> 170
