-- SPEC 5.3: 1,050 requisitions: filled 900, open at 2025-12-31 90, cancelled 60.
with by_status as (
    select status, count(*) as n from {{ ref('fct_requisitions') }} group by status
),
expected as (
    select 'filled' as status, 900 as n
    union all select 'open', 90
    union all select 'cancelled', 60
)
select e.status, e.n as expected_n, b.n as actual_n
from expected as e
full outer join by_status as b on b.status = e.status
where b.n is null or e.n is null or b.n <> e.n
union all
select 'total', 1050, count(*) from {{ ref('fct_requisitions') }} having count(*) <> 1050
union all
select 'semantic_filled', 900, count(*) from {{ ref('sem_requisitions') }} having count(*) <> 900
