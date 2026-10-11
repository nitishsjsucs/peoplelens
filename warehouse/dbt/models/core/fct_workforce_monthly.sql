-- One row per employee per month in which the employee was employed at any
-- point (including the month of termination). Attributes are taken as of the
-- reference date: the month end, or the termination date if earlier.
-- Active on day d means hire_date <= d and (termination_date is null or
-- termination_date > d): the termination day itself is not an active day.
with months as (
    select month_start, month_end from {{ ref('dim_month') }}
),

spans as (
    select
        e.employee_id,
        e.hire_date,
        e.termination_date,
        e.termination_type,
        e.employment_type,
        e.fte,
        m.month_start,
        m.month_end,
        case
            when e.termination_date is not null and e.termination_date < m.month_end then e.termination_date
            else m.month_end
        end as ref_date
    from {{ ref('dim_employee') }} as e
    inner join months as m
        on e.hire_date <= m.month_end
        and (e.termination_date is null or e.termination_date >= m.month_start)
),

with_job as (
    select
        s.*,
        j.department,
        j.location,
        j.job_level,
        j.job_family
    from spans as s
    inner join {{ ref('stg_job_history') }} as j
        on j.employee_id = s.employee_id
        and j.effective_date <= s.ref_date
        and (j.end_date is null or j.end_date >= s.ref_date)
),

salary_dates as (
    select
        w.employee_id,
        w.month_start,
        max(c.effective_date) as salary_effective_date
    from with_job as w
    inner join {{ ref('stg_compensation') }} as c
        on c.employee_id = w.employee_id
        and c.effective_date <= w.ref_date
    group by w.employee_id, w.month_start
),

final as (
    select
        w.employee_id,
        w.month_start,
        w.department,
        w.location,
        w.job_level,
        w.job_family,
        w.employment_type,
        {{ pl_days_between('w.hire_date', 'w.month_end') }} as tenure_days,
        case
            when w.termination_date is null or w.termination_date > w.month_end then 1
            else 0
        end as is_active_eom,
        w.fte as fte_eom,
        case
            when w.termination_date >= w.month_start and w.termination_date <= w.month_end then 1
            else 0
        end as is_termination,
        case
            when w.termination_date >= w.month_start and w.termination_date <= w.month_end
                and w.termination_type = 'voluntary' then 1
            else 0
        end as is_voluntary_termination,
        c.base_salary_usd as base_salary_eom
    from with_job as w
    inner join salary_dates as sd
        on sd.employee_id = w.employee_id
        and sd.month_start = w.month_start
    inner join {{ ref('stg_compensation') }} as c
        on c.employee_id = sd.employee_id
        and c.effective_date = sd.salary_effective_date
)

select
    {{ pl_row_uuid() }} as workforce_key,  -- random per build, never derived from employee_id
    employee_id,
    {{ pl_time('month_start') }} as month_start,
    department,
    location,
    job_level,
    job_family,
    employment_type,
    case
        when tenure_days < 365 then '<1y'
        when tenure_days < 730 then '1-2y'
        when tenure_days < 1826 then '2-5y'
        else '5y+'
    end as tenure_band,
    is_active_eom,
    fte_eom,
    is_termination,
    is_voluntary_termination,
    base_salary_eom
from final
