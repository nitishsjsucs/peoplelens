select
    employee_id,
    cast(effective_date as date) as effective_date,
    cast(base_salary_usd as {{ dbt.type_bigint() }}) as base_salary_usd,
    change_reason
from {{ ref('raw_compensation_history') }}
