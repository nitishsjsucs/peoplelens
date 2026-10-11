select
    requisition_id,
    department,
    location,
    job_level,
    cast(opened_date as date) as opened_date,
    status,
    cast(filled_date as date) as filled_date,
    cast(closed_date as date) as closed_date
from {{ ref('raw_requisitions') }}
