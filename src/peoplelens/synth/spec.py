"""Every constant of the synthetic dataset, in one place.

Tests import these values, so each number appears exactly once in the repo.
SPEC.md section 5 is the source of truth for the values below.
"""

from __future__ import annotations

from datetime import date

SEED = 20231

COMPANY = "Lumen Works"
EMAIL_DOMAIN = "lumenworks.example"
CANDIDATE_EMAIL_DOMAIN = "mail.example"

WINDOW_START = date(2023, 1, 1)
WINDOW_END = date(2025, 12, 31)
AS_OF = date(2025, 12, 31)
WINDOW_YEARS = (2023, 2024, 2025)

OPENER_HIRE_START = date(2008, 1, 1)
OPENER_HIRE_END = date(2022, 12, 31)
REQ_OPEN_START = date(2022, 9, 1)

# Headcount flow
TOTAL_EMPLOYEES = 2400
OPENING_HEADCOUNT = 1500
HIRES_BY_YEAR = {2023: 280, 2024: 300, 2025: 320}
TERMINATIONS_BY_YEAR = {2023: 190, 2024: 210, 2025: 220}
VOLUNTARY_TERMINATIONS = 450
INVOLUNTARY_TERMINATIONS = 170
CLOSING_HEADCOUNT = 1780

# Window hires who also leave inside the window, by (hire year, termination year).
TERMINATED_HIRES = {
    (2023, 2023): 15,
    (2023, 2024): 20,
    (2023, 2025): 15,
    (2024, 2024): 20,
    (2024, 2025): 25,
    (2025, 2025): 25,
}

# Employment type and FTE
FTE_FULL = "1.0"
FTE_FOUR_FIFTHS = "0.8"
FTE_HALF = "0.5"
CLOSING_PART_TIME_HALF = 80
CLOSING_PART_TIME_FOUR_FIFTHS = 80
CLOSING_PART_TIME = CLOSING_PART_TIME_HALF + CLOSING_PART_TIME_FOUR_FIFTHS
CLOSING_FTE = 1724.0
TERMINATED_PART_TIME_HALF = 20
TERMINATED_PART_TIME_FOUR_FIFTHS = 20

# Closing (2025-12-31) marginals, in display order.
CLOSING_BY_DEPARTMENT = {
    "Engineering": 620,
    "Sales": 380,
    "Customer Success": 250,
    "Marketing": 150,
    "Finance": 120,
    "People": 90,
    "Operations": 162,
    "Legal": 8,
}
CLOSING_BY_LOCATION = {
    "San Jose": 700,
    "New York": 420,
    "Austin": 300,
    "London": 220,
    "Bengaluru": 140,
}
CLOSING_BY_LEVEL = {
    "L1": 300,
    "L2": 520,
    "L3": 480,
    "L4": 290,
    "L5": 140,
    "L6": 50,
}

DEPARTMENTS = tuple(CLOSING_BY_DEPARTMENT)
LOCATIONS = tuple(CLOSING_BY_LOCATION)
JOB_LEVELS = tuple(CLOSING_BY_LEVEL)
EMPLOYMENT_TYPES = ("full_time", "part_time")
TENURE_BANDS = ("<1y", "1-2y", "2-5y", "5y+")
# Tenure band upper bounds in days since hire, measured at the month end.
TENURE_BAND_DAYS = ((365, "<1y"), (730, "1-2y"), (1826, "2-5y"))
TENURE_BAND_OPEN = "5y+"

JOB_FAMILIES = {
    "Engineering": ("Software Engineering", "Data Engineering", "Site Reliability"),
    "Sales": ("Account Executive", "Sales Development"),
    "Customer Success": ("Customer Success Management", "Support"),
    "Marketing": ("Product Marketing", "Demand Generation"),
    "Finance": ("Accounting", "Financial Planning"),
    "People": ("People Partners", "Recruiting"),
    "Operations": ("Business Operations", "IT Operations"),
    "Legal": ("Legal Counsel",),
}

# Job changes inside the window
PROMOTIONS = 300
TRANSFERS = 120
JOB_HISTORY_ROWS = TOTAL_EMPLOYEES + PROMOTIONS + TRANSFERS  # 2,820

# Recruiting
REQUISITIONS = 1050
REQUISITIONS_FILLED = 900
REQUISITIONS_OPEN = 90
REQUISITIONS_CANCELLED = 60
OFFERS = 1080
OFFERS_ACCEPTED = 900
OFFERS_DECLINED = 180
DECLINED_ON_FILLED = 120
DECLINED_ON_OPEN = 40
DECLINED_ON_CANCELLED = 20
CANDIDATES = OFFERS

# Time to fill bounds in days, by level of the requisition.
TIME_TO_FILL_DAYS = {
    "L1": (35, 60),
    "L2": (40, 70),
    "L3": (45, 80),
    "L4": (55, 95),
    "L5": (65, 110),
    "L6": (75, 120),
}

# Compensation model (integer USD, rounded to 100).
BASE_SALARY_BY_LEVEL = {"L1": 85000, "L2": 110000, "L3": 140000, "L4": 175000, "L5": 215000, "L6": 270000}
DEPARTMENT_PAY_BP = {
    "Engineering": 11000,
    "Sales": 9500,
    "Customer Success": 8500,
    "Marketing": 9500,
    "Finance": 10000,
    "People": 9000,
    "Operations": 8500,
    "Legal": 11500,
}
LOCATION_PAY_BP = {"San Jose": 11500, "New York": 11200, "Austin": 9800, "London": 9500, "Bengaluru": 4500}
SALARY_SPREAD_BP = (8500, 11500)
MERIT_BP = (200, 500)
PROMOTION_BP = (800, 1500)
MERIT_MONTH_DAY = (4, 1)
MERIT_MIN_TENURE_MONTHS = 6

# Pinned after the first deterministic run (SPEC.md section 5.2 note): hire rows
# (2,400) + April 1 merit rows (4,496) + promotion rows (300). Change only in a
# deliberate commit that regenerates the seeds.
COMPENSATION_ROWS = 7196

SEED_FILES = (
    "raw_employees.csv",
    "raw_job_history.csv",
    "raw_compensation_history.csv",
    "raw_requisitions.csv",
    "raw_offers.csv",
    "raw_candidates.csv",
)
