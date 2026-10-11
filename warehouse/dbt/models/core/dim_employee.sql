select
    employee_id,
    hire_date,
    termination_date,
    termination_type,
    employment_type,
    fte
from {{ ref('stg_employees') }}
