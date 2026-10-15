"""Constants for the NZ Contact Energy integration."""
# Entity Names / Descriptive Constants
from datetime import date

SENSOR_USAGE_NAME = "NZ Contact Energy Consumption"

DOMAIN = "nz_contact_energy"

CONF_ACCOUNT_ID = "account_id"
CONF_PASSWORD = "password"
CONF_REFRESH_INTERVAL = "refresh_interval"
CONF_FIRST_DAY_HISTORY =  "first_day_history"   # "YYYY-MM-DD"
CONF_DELTA_NO_DATA_DAYS = "delta_no_data_days"  # int

# Default Values
DEFAULT_REFRESH_INTERVAL = 24
DEFAULT_DELTA_NO_DATA_DAYS = 2

def default_first_day_history() -> str:
    """First day of the month, two months ago."""
    today = date.today()
    # First day of this month, then go back 2 months
    first_this_month = today.replace(day=1)
    year = first_this_month.year
    month = first_this_month.month - 2
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1).isoformat()
