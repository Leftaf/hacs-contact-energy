"""Contact Energy sensors."""

from datetime import datetime, timedelta
import logging
import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, UnitOfEnergy

from homeassistant.components.recorder.models import ( 
    StatisticData, 
    StatisticMetaData, 
    StatisticMeanType,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
)

from .api import ContactEnergyApi
from .const import (
    DOMAIN,
    SENSOR_USAGE_NAME,
    CONF_USAGE_DAYS,
)

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_USAGE_DAYS, default=10): cv.positive_int,
    }
)

SCAN_INTERVAL = timedelta(hours=3)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the platform async."""
    email = config.get(CONF_EMAIL)
    password = config.get(CONF_PASSWORD)
    usage_days = config.get(CONF_USAGE_DAYS)

    api = ContactEnergyApi(email, password)

    _LOGGER.debug("Setting up sensor(s)...")

    # Set update_before_add=False to prevent startup blocking
    async_add_entities([ContactEnergyUsageSensor(SENSOR_USAGE_NAME, api, usage_days)], False)


class ContactEnergyUsageSensor(SensorEntity):
    """Define Contact Energy Usage sensor."""

    _attr_icon = "mdi:meter-electric"
    _attr_native_value = 0
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_unique_id = DOMAIN
    _attr_device_class = "energy"
    _attr_state_class = "total"

    def __init__(self, name, api, usage_days):
        """Initialise the sensor."""
        self._attr_name = name
        self._usage_days = usage_days
        self._api = api

    def _fetch_data_and_process(self):
        """Synchronous wrapper for blocking API calls."""
        if not self._api._api_token:
            _LOGGER.info("Haven't logged in yet, logging in now...")
            if not self._api.login():
                _LOGGER.error("Failed to login - check credentials")
                return None, None

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        kWhStatistics = []
        freeKWhStatistics = []
        kWhRunningSum = 0
        freeKWhRunningSum = 0

        for i in range(self._usage_days):
            previous_day = today - timedelta(days=self._usage_days - i)
            response = self._api.get_usage(
                str(previous_day.year), str(previous_day.month), str(previous_day.day)
            )
            if response and response[0]:
                for point in response:
                    if point.get("value"):
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
        _LOGGER.debug("Beginning usage update")

        # Offload blocking network calls to executor pool
        kWhStatistics, freeKWhStatistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process
        )

        if kWhStatistics is None:
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