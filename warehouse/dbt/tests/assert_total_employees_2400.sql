-- SPEC 5.3: employees ever employed in the dataset.
select count(*) as n
from {{ ref('dim_employee') }}
having count(*) <> 2400
