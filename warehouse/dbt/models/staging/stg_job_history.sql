select
    employee_id,
    cast(effective_date as date) as effective_date,
    cast(end_date as date) as end_date,
    department,
    location,
    job_level,
    job_family,
    change_reason
from {{ ref('raw_job_history') }}
