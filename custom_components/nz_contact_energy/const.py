"""Constants for the Contact Energy sensors."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)
DOMAIN = "contact_energy"

SENSOR_USAGE_NAME = "Contact Energy Usage"
CONF_USAGE_DAYS = "usage_days"
CONF_REFRESH_INTERVAL = "refresh_interval"
CONF_ACCOUNT_ID = "account_id"

DEFAULT_USAGE_DAYS = 10
DEFAULT_REFRESH_INTERVAL = 24