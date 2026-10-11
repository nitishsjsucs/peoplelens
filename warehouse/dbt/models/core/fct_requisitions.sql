with reqs as (
    select * from {{ ref('stg_requisitions') }}
)

select
    requisition_id,
    department,
    location,
    job_level,
    opened_date,
    {{ pl_time('filled_date') }} as filled_date,
    status,
    case
        when status = 'filled' then {{ pl_days_between('opened_date', 'reqs.filled_date') }}
    end as days_to_fill
from reqs
