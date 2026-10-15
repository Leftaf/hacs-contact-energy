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
from homeassistant.components.recorder.statistics import async_add_external_statistics
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

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Contact Energy sensor from a config entry."""
    config = {**entry.data, **entry.options}

    api = ContactEnergyApi(config[CONF_ACCOUNT_ID], config[CONF_PASSWORD])

    first_day_history = config.get(CONF_FIRST_DAY_HISTORY, default_first_day_history())
    delta_no_data_days = config.get(CONF_DELTA_NO_DATA_DAYS, DEFAULT_DELTA_NO_DATA_DAYS)
    refresh_hours = config.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)

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
        self._delta_no_data_days = max(1, int(delta_no_data_days))
        self._refresh_hours = max(1, int(refresh_hours))
        self._entry_id = entry_id
        # Note: SCAN_INTERVAL is mainly read from the class; reload after options change
        self.__class__.SCAN_INTERVAL = timedelta(hours=self._refresh_hours)

    def _date_range(self) -> tuple[date, date]:
        """Return inclusive (start, end). end is always yesterday."""
        today = dt_util.now().date()
        end = today - timedelta(days=1)
        first = date.fromisoformat(self._first_day_history)

        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})

        if not store.get("history_imported"):
            # First successful run: full backfill from configured first day
            start = first
        else:
            # Later runs: only the lag window
            start = max(first, today - timedelta(days=self._delta_no_data_days))

        start = min(start, end)
        return start, end

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

        if not self._api._api_token or not self._api._contract_id:
            _LOGGER.debug("No valid session – logging in")
            if not self._api.login():
                _LOGGER.error("Failed to login - check credentials")
                return None, None
        else:
            _LOGGER.debug("Existing session present – continuing")

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

        # Optional: expose last total on the entity
        self._attr_native_value = round(kwh_running_sum, 2)

        return kwh_statistics, free_kwh_statistics

    async def async_update(self) -> None:
        """Begin usage update asynchronously."""
        kwh_statistics, free_kwh_statistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process
        )

        # Login failed
        if kwh_statistics is None:
            return

        # Mark history as imported (survives restart via config entry data)
        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})
        if not store.get("history_imported"):
            store["history_imported"] = True
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, "history_imported": True},
                )
            _LOGGER.info("Historical import flag set for entry %s", self._entry_id)

        if not kwh_statistics and not free_kwh_statistics:
            return

        if kwh_statistics:
            kwh_metadata: StatisticMetaData = {
                "has_sum": True,
                "name": "NZ Contact Energy Consumption",
                "source": DOMAIN,
                "statistic_id": f"{DOMAIN}:energy_consumption",
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                "mean_type": StatisticMeanType.NONE,
                "unit_class": "energy",
            }
            async_add_external_statistics(self.hass, kwh_metadata, kwh_statistics)

        if free_kwh_statistics:
            free_kwh_metadata: StatisticMetaData = {
                "has_sum": True,
                "name": "Free NZ Contact Energy Consumption",
                "source": DOMAIN,
                "statistic_id": f"{DOMAIN}:free_energy_consumption",
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                "mean_type": StatisticMeanType.NONE,
                "unit_class": "energy",
            }
            async_add_external_statistics(
                self.hass, free_kwh_metadata, free_kwh_statistics
            )
