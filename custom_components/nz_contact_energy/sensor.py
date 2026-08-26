"""Contact Energy sensors."""

from datetime import datetime, timedelta
import logging

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ContactEnergyApi
from .const import (
    CONF_REFRESH_INTERVAL,
    CONF_USAGE_DAYS,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_USAGE_DAYS,
    DOMAIN,
    SENSOR_USAGE_NAME,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Contact Energy sensor from a config entry."""
    config = entry.data
    api = ContactEnergyApi(config[CONF_EMAIL], config[CONF_PASSWORD])
    usage_days = config.get(CONF_USAGE_DAYS, DEFAULT_USAGE_DAYS)
    refresh_hours = config.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)

    sensor = ContactEnergyUsageSensor(SENSOR_USAGE_NAME, api, usage_days, refresh_hours)
    async_add_entities([sensor], False)


class ContactEnergyUsageSensor(SensorEntity):
    """Define Contact Energy Usage sensor."""

    _attr_icon = "mdi:meter-electric"
    _attr_native_value = 0
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_unique_id = DOMAIN
    _attr_device_class = "energy"
    _attr_state_class = "total"

    def __init__(self, name: str, api: ContactEnergyApi, usage_days: int, refresh_hours: int):
        """Initialise the sensor."""
        self._attr_name = name
        self._usage_days = usage_days
        self._api = api
        self._scan_interval = timedelta(hours=refresh_hours)

    @property
    def scan_interval(self) -> timedelta:
        """Dynamic polling interval set via config UI."""
        return self._scan_interval

    def _fetch_data_and_process(self):
        """Synchronous wrapper for blocking API calls."""
        if not self._api._api_token:
            if not self._api.login():
                _LOGGER.error("Failed to login - check credentials")
                return None, None

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        kWhStatistics = []
        freeKWhStatistics = []
        kWhRunningSum = 0.0
        freeKWhRunningSum = 0.0

        for i in range(self._usage_days):
            previous_day = today - timedelta(days=self._usage_days - i)
            response = self._api.get_usage(
                str(previous_day.year), f"{previous_day.month:02d}", f"{previous_day.day:02d}"
            )
            if response and isinstance(response, list):
                for point in response:
                    if isinstance(point, dict) and point.get("value"):
                        if point.get("offpeakValue") == "0.00":
                            kWhRunningSum += float(point["value"])
                        else:
                            freeKWhRunningSum += float(point["value"])

                        dt = datetime.strptime(point["date"], "%Y-%m-%dT%H:%M:%S.%f%z")
                        freeKWhStatistics.append(StatisticData(start=dt, sum=freeKWhRunningSum))
                        kWhStatistics.append(StatisticData(start=dt, sum=kWhRunningSum))

        return kWhStatistics, freeKWhStatistics

    async def async_update(self):
        """Begin usage update asynchronously."""
        kWhStatistics, freeKWhStatistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process
        )

        if not kWhStatistics:
            return

        kWhMetadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="ContactEnergy",
            source=DOMAIN,
            statistic_id=f"{DOMAIN}:energy_consumption",
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            mean_type=StatisticMeanType.NONE,
            unit_class="energy",
        )
        async_add_external_statistics(self.hass, kWhMetadata, kWhStatistics)

        freeKWHMetadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name="FreeContactEnergy",
            source=DOMAIN,
            statistic_id=f"{DOMAIN}:free_energy_consumption",
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            mean_type=StatisticMeanType.NONE,
            unit_class="energy",
        )
        async_add_external_statistics(self.hass, freeKWHMetadata, freeKWhStatistics)