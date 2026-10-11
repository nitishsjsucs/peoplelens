-- PII (full_name, work_email, date_of_birth, gender) is dropped here.
select
    employee_id,
    cast(hire_date as date) as hire_date,
    cast(termination_date as date) as termination_date,
    nullif(termination_type, '') as termination_type,
    employment_type,
    cast(fte as {{ pl_type_double() }}) as fte
from {{ ref('raw_employees') }}
