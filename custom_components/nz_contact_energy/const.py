"""Constants for the NZ Contact Energy integration."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path


def default_first_day_history() -> str:
    """Return the first day of the month, two months ago."""
    today = datetime.now(tz=UTC).date()
    first_this_month = today.replace(day=1)
    year = first_this_month.year
    month = first_this_month.month - 2
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1).isoformat()


def _load_version() -> str:
    """Read version from manifest.json (single source of truth for releases)."""
    try:
        manifest_path = Path(__file__).resolve().parent / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return str(data.get("version", "0.0.0"))
    except OSError, json.JSONDecodeError, TypeError:
        return "0.0.0"


SENSOR_USAGE_NAME = "NZ Contact Energy Consumption"
SENSOR_COST_NAME = "NZ Contact Energy Cost (excl. GST)"

DOMAIN = "nz_contact_energy"
VERSION = _load_version()

CONF_ACCOUNT_ID = "account_id"
CONF_PASSWORD = "password"
CONF_REFRESH_INTERVAL = "refresh_interval"
CONF_FIRST_DAY_HISTORY = "first_day_history"  # "YYYY-MM-DD"
CONF_DELTA_NO_DATA_DAYS = "delta_no_data_days"  # int

DEFAULT_REFRESH_INTERVAL = 24
DEFAULT_DELTA_NO_DATA_DAYS = 2
