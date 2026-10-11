-- SPEC 5.3: active on 2023-01-01 (hired before the window, not yet terminated).
select count(*) as n
from {{ ref('dim_employee') }}
where hire_date < cast('2023-01-01' as date)
  and (termination_date is null or termination_date >= cast('2023-01-01' as date))
having count(*) <> 1500
