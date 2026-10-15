"""NZ Contact Energy sensors."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_PASSWORD, UnitOfEnergy
from homeassistant.util import dt as dt_util

from .api import ContactEnergyApi
from .const import (
    CONF_ACCOUNT_ID,
    CONF_DELTA_NO_DATA_DAYS,
    CONF_FIRST_DAY_HISTORY,
    CONF_REFRESH_INTERVAL,
    DEFAULT_DELTA_NO_DATA_DAYS,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    SENSOR_USAGE_NAME,
    default_first_day_history,
)
from custom_components.nz_contact_energy import api

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)


def _date_range(self) -> tuple[date, date]:
    today = dt_util.now().date()
    end = today - timedelta(days=1)
    first = date.fromisoformat(self._first_day_history)
    delta_start = today - timedelta(days=self._delta_no_data_days)

    start = max(first, delta_start)
    start = min(start, end)

    store = self.hass.data[DOMAIN][self._entry_id]
    if not store.get("history_imported"):
        # First successful full import window
        start = first
    else:
        start = max(first, today - timedelta(days=self._delta_no_data_days))

    return start, end


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # Options override the original config data
    config = {**entry.data, **entry.options}

    api = ContactEnergyApi(config[CONF_ACCOUNT_ID], config[CONF_PASSWORD])

    first_day_history = config.get(
        CONF_FIRST_DAY_HISTORY, default_first_day_history()
    )
    delta_no_data_days = config.get(
        CONF_DELTA_NO_DATA_DAYS, DEFAULT_DELTA_NO_DATA_DAYS
    )
    refresh_hours = config.get(
        CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL
    )
    sensor = ContactEnergyUsageSensor(
        SENSOR_USAGE_NAME,
        api,
        first_day_history,
        delta_no_data_days,
        refresh_hours,
        entry.entry_id,
    )
    async_add_entities([sensor], update_before_add=True)


class ContactEnergyUsageSensor(SensorEntity):
    """Define Contact Energy Usage sensor."""

    # Fallback if nothing is configured
    SCAN_INTERVAL = timedelta(hours=DEFAULT_REFRESH_INTERVAL)

    _attr_icon = "mdi:meter-electric"
    _attr_native_value = 0
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_unique_id = DOMAIN
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        name: str,
        api: ContactEnergyApi,
        first_day_history: str,
        delta_no_data_days: int,
        refresh_hours: int,
        entry_id: str,
    ) -> None:
        """Initialise the sensor."""
        self._attr_name = name
        self._api = api
        self._first_day_history = first_day_history
        self._delta_no_data_days = delta_no_data_days
        self._refresh_hours = refresh_hours
        self._entry_id = entry_id

        # Fallback if nothing is configured
        self.SCAN_INTERVAL = timedelta(hours=max(1, refresh_hours))

    def _fetch_data_and_process(
        self,
    ) -> tuple[list[StatisticData] | None, list[StatisticData] | None]:
        """Fetch and process data using blocking API calls."""
        start, end = self._date_range()

        _LOGGER.debug(
            "token=%s contract=%s account=%s range=%s → %s",
            bool(self._api._api_token),
            self._api._contract_id,
            self._api._account_id,
            start,
            end,
        )
        # Ensure we have a valid session
        if not self._api._api_token or not self._api._contract_id:
            _LOGGER.debug("No valid session – logging in")
            if not self._api.login():
                _LOGGER.error("Failed to login - check credentials")
                return None, None
        else:
            _LOGGER.debug("Existing session present – continuing")

        today = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
        kwh_statistics: list[StatisticData] = []
        free_kwh_statistics: list[StatisticData] = []
        kwh_running_sum = 0.0
        free_kwh_running_sum = 0.0

        _LOGGER.debug("Starting usage fetch from %s to %s", start, end)

        day = start

        while day <= end:
            try:
                response = self._api.get_usage(day.year, day.month, day.day)

                time.sleep(1.5)

                if not response or not isinstance(response, list):
                    _LOGGER.warning("No usable data for %s", day)
                    day += timedelta(days=1)
                    continue

                _LOGGER.info("Day %s → received %d points", day, len(response))

                for point in response:
                    if not isinstance(point, dict) or not point.get("value"):
                        continue

                    try:
                        val = float(point["value"])
                        dt = datetime.fromisoformat(point["date"])

                        if point.get("offpeakValue") == "0.00":
                            kwh_running_sum += val
                            kwh_statistics.append(
                                StatisticData(start=dt, sum=kwh_running_sum)
                            )
                        else:
                            free_kwh_running_sum += val
                            free_kwh_statistics.append(
                                StatisticData(start=dt, sum=free_kwh_running_sum)
                            )

                    except (ValueError, TypeError, KeyError) as err:
                        _LOGGER.warning("Skipping bad data point on %s: %s", day, err)

            except Exception:
                _LOGGER.exception("Error processing day %s", day)

            day += timedelta(days=1)

        _LOGGER.info(
            "Finished processing %s → %s: %d normal points, %d free points",
            start,
            end,
            len(kwh_statistics),
            len(free_kwh_statistics),
        )
        return kwh_statistics, free_kwh_statistics

    async def async_update(self) -> None:
        """Begin usage update asynchronously."""
        kwh_statistics, free_kwh_statistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process
        )

        if not kwh_statistics:
            return

        kwh_metadata: StatisticMetaData = {
            "has_sum": True,
            "name": "NZ Contact Energy Consumption",
            "source": DOMAIN,
            "statistic_id": f"{DOMAIN}:energy_consumption",
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            "mean_type": StatisticMeanType.NONE,
            "unit_class": "energy"
        }
        async_add_external_statistics(
            self.hass, kwh_metadata, kwh_statistics
        )

        if free_kwh_statistics:
            free_kwh_metadata: StatisticMetaData = {
                "has_sum": True,
                "name": "Free NZ Contact Energy Consumption",
                "source": DOMAIN,
                "statistic_id": f"{DOMAIN}:free_energy_consumption",
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                "mean_type": StatisticMeanType.NONE,
                "unit_class": "energy"
            }
            async_add_external_statistics(
                self.hass, free_kwh_metadata, free_kwh_statistics
            )
