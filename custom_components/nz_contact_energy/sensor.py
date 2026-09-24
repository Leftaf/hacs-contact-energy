"""NZ Contact Energy sensors."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.recorder.models import (
    StatisticData,
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
    CONF_REFRESH_INTERVAL,
    CONF_USAGE_DAYS,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_USAGE_DAYS,
    DOMAIN,
    SENSOR_USAGE_NAME,
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
    config = entry.data
    api = ContactEnergyApi(config[CONF_ACCOUNT_ID], config[CONF_PASSWORD])
    usage_days = config.get(CONF_USAGE_DAYS, DEFAULT_USAGE_DAYS)
    refresh_hours = config.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)

    sensor = ContactEnergyUsageSensor(SENSOR_USAGE_NAME, api, usage_days, refresh_hours)
    async_add_entities([sensor], update_before_add=True)


class ContactEnergyUsageSensor(SensorEntity):
    """Define Contact Energy Usage sensor."""

    _attr_icon = "mdi:meter-electric"
    _attr_native_value = 0
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_unique_id = DOMAIN
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self, name: str, api: ContactEnergyApi, usage_days: int, refresh_hours: int
    ) -> None:
        """Initialise the sensor."""
        self._attr_name = name
        self._usage_days = usage_days
        self._api = api
        self._refresh_hours = refresh_hours

    def _fetch_data_and_process(
        self,
    ) -> tuple[list[StatisticData] | None, list[StatisticData] | None]:
        """Fetch and process data using blocking API calls."""
        # Only log in if we don't already have valid tokens/account info
        if not getattr(self._api, "_contract_id", None) and not self._api.login():
            _LOGGER.error("Failed to login - check credentials")
            return None, None

        today = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
        kwh_statistics: list[StatisticData] = []
        free_kwh_statistics: list[StatisticData] = []
        kwh_running_sum = 0.0
        free_kwh_running_sum = 0.0

        for i in range(self._usage_days):
            previous_day = today - timedelta(days=self._usage_days - i)
            response = self._api.get_usage(
                previous_day.year,
                previous_day.month,
                previous_day.day,
            )
            if response and isinstance(response, list):
                for point in response:
                    if isinstance(point, dict) and point.get("value"):
                        val = float(point["value"])
                        if point.get("offpeakValue") == "0.00":
                            kwh_running_sum += val
                        else:
                            free_kwh_running_sum += val

                        dt = datetime.strptime(point["date"], "%Y-%m-%dT%H:%M:%S.%f%z")
                        free_kwh_statistics.append(
                            StatisticData(start=dt, sum=free_kwh_running_sum)
                        )
                        kwh_statistics.append(
                            StatisticData(start=dt, sum=kwh_running_sum)
                        )

        return kwh_statistics, free_kwh_statistics

    async def async_update(self) -> None:
        """Begin usage update asynchronously."""
        kwh_statistics, free_kwh_statistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process
        )

        if not kwh_statistics:
            return

        kwh_metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="NZ Contact Energy Consumption",
            source=DOMAIN,
            statistic_id=f"{DOMAIN}:energy_consumption",
            unit_of_measurement=(UnitOfEnergy.KILO_WATT_HOUR),
        )
        async_add_external_statistics(self.hass, kwh_metadata, kwh_statistics)

        if free_kwh_statistics:
            free_kwh_metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name="Free NZ Contact Energy Consumption",
                source=DOMAIN,
                statistic_id=f"{DOMAIN}:free_energy_consumption",
                unit_of_measurement=str(UnitOfEnergy.KILO_WATT_HOUR),
            )
            async_add_external_statistics(
                self.hass, free_kwh_metadata, free_kwh_statistics
            )
