-- candidate_id is dropped: nothing downstream needs a person-level candidate key.
select
    offer_id,
    requisition_id,
    cast(offer_date as date) as offer_date,
    decision,
    cast(decision_date as date) as decision_date,
    nullif(hired_employee_id, '') as hired_employee_id
from {{ ref('raw_offers') }}
