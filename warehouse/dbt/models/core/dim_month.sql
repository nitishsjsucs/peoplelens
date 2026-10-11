-- The 36 months of the analysis window, 2023-01 to 2025-12.
with months as (
    {{ pl_month_series('2023-01-01', '2025-12-01') }}
)
select
    month_start,
    {{ pl_month_end('month_start') }} as month_end
from months
