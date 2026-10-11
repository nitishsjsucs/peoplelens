-- SPEC 5.3: offers extended 1,080, accepted 900, declined 180.
select count(*) as extended, sum(is_accepted) as accepted
from {{ ref('fct_offers') }}
having count(*) <> 1080 or sum(is_accepted) <> 900 or count(*) - sum(is_accepted) <> 180
