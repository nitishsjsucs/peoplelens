select
    o.offer_id,
    o.requisition_id,
    r.department,
    r.location,
    r.job_level,
    {{ pl_time('o.offer_date') }} as offer_date,
    case when o.decision = 'accepted' then 1 else 0 end as is_accepted
from {{ ref('stg_offers') }} as o
inner join {{ ref('stg_requisitions') }} as r
    on r.requisition_id = o.requisition_id
