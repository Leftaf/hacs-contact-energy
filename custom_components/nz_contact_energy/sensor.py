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
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.util import dt as dt_util

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_PASSWORD, UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.recorder import get_instance

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

STATISTIC_ID = f"{DOMAIN}:energy_consumption"
FREE_STATISTIC_ID = f"{DOMAIN}:free_energy_consumption"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Contact Energy sensor — do not block on history."""
    config = {**entry.data, **entry.options}

    api = ContactEnergyApi(config[CONF_ACCOUNT_ID], config[CONF_PASSWORD])

    sensor = ContactEnergyUsageSensor(
        name=SENSOR_USAGE_NAME,
        api=api,
        first_day_history=config.get(
            CONF_FIRST_DAY_HISTORY, default_first_day_history()
        ),
        delta_no_data_days=config.get(
            CONF_DELTA_NO_DATA_DAYS, DEFAULT_DELTA_NO_DATA_DAYS
        ),
        refresh_hours=config.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL),
        entry_id=entry.entry_id,
    )
    # False = setup returns immediately; data loads in background
    async_add_entities([sensor], update_before_add=False)

    # Make it reachable from the service
    hass.data[DOMAIN][entry.entry_id]["sensor"] = sensor


class ContactEnergyUsageSensor(SensorEntity):
    """Contact Energy usage → external statistics for the Energy dashboard."""

    _attr_should_poll = False

    _attr_icon = "mdi:meter-electric"
    _attr_native_value = 0
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_unique_id = DOMAIN
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_has_entity_name = True
    _attr_name = "Consumption"

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
        self._import_task = None
        self._unsub_interval = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=f"Account {self._api._account_id}",
            manufacturer="Home Assistant Community",
            model=f"Contract {self._api._contract_id}",
            serial_number=str(self._api._contract_id),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://github.com/Leftaf/hacs-contact-energy",
            sw_version="0.1.0",
        )
    @property
    def extra_state_attributes(self) -> dict[str, Any]: # type: ignore  # noqa: F821
        """Return the state attributes."""
        return {
            "account": self._api._account_id,
            "contract_id": self._api._contract_id,
            "first_day_history": self._first_day_history,
            "delta_no_data_days": self._delta_no_data_days,
            "refresh_interval_hours": self._refresh_hours,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Entity is in HA — start history import or a short refresh."""
        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})

        if not store.get("history_imported"):
            _LOGGER.info(
                "Scheduling background historical import from %s",
                self._first_day_history,
            )
            self._import_task = self.hass.async_create_task(
                self._async_import_history()
            )
        else:
            # Quick lag-window update only
            self.hass.async_create_task(self.async_update_ha_state(force_refresh=True))

        # call only once per interval, not on every HA 30 sec update
        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._async_interval_update,
            timedelta(hours=self._refresh_hours),
        )

    async def _async_interval_update(self, _now=None) -> None:
        """Called on the configured interval."""
        await self.async_update_ha_state(force_refresh=True)

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

        if self._import_task is not None and not self._import_task.done():
            self._import_task.cancel()
            self._import_task = None

        if hasattr(self._api, "close"):
            self._api.close() # pyright: ignore[reportAttributeAccessIssue]


    # ------------------------------------------------------------------
    # Date range
    # ------------------------------------------------------------------

    def _date_range(self, *, full_history: bool = False) -> tuple[date, date]:
        """
        Return the inclusive date range, ending yesterday.

        Full history starts at first_day_history; normal polls use the lag window.
        """
        today = dt_util.now().date()
        end = today - timedelta(days=1)
        first = date.fromisoformat(self._first_day_history)

        if full_history:
            start = first
        else:
            start = max(first, today - timedelta(days=self._delta_no_data_days))

        start = min(start, end)
        return start, end

    # ------------------------------------------------------------------
    # Blocking fetch (runs in executor)
    # ------------------------------------------------------------------

    def _fetch_data_and_process(
        self,
        full_history: bool = False,
        start_date: date | None = None,
        end_date: date | None = None,
        kwh_seed: float = 0.0,
        free_seed: float = 0.0,
    ) -> tuple[list[StatisticData] | None, list[StatisticData] | None]:
        """Fetch usage and build statistic points. Blocking — executor only."""

        if start_date is not None:
            start = start_date
            end = end_date or start_date
        else:
            start, end = self._date_range(full_history=full_history)

        _LOGGER.debug(
            "token=%s contract=%s account=%s range=%s → %s (full_history=%s)",
            bool(self._api._api_token),
            self._api._contract_id,
            self._api._account_id,
            start,
            end,
            full_history,
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
        kwh_running_sum = kwh_seed
        free_kwh_running_sum = free_seed

        _LOGGER.info("Starting usage fetch from %s to %s", start, end)

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
            "Finished processing %s → %s: %d normal, %d free",
            start,
            end,
            len(kwh_statistics),
            len(free_kwh_statistics),
        )
        self._attr_native_value = round(kwh_running_sum, 2)
        return kwh_statistics, free_kwh_statistics

    # ------------------------------------------------------------------
    # Write statistics (main thread)
    # ------------------------------------------------------------------

    def _write_statistics(
        self,
        kwh_statistics: list[StatisticData],
        free_kwh_statistics: list[StatisticData],
    ) -> None:
        """Push external statistics into the recorder."""
        if kwh_statistics:
            metadata: StatisticMetaData = {
                "has_sum": True,
                "name": "NZ Contact Energy Consumption",
                "source": DOMAIN,
                "statistic_id": f"{DOMAIN}:energy_consumption",
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                "mean_type": StatisticMeanType.NONE,
                "unit_class": "energy",
            }
            async_add_external_statistics(self.hass, metadata, kwh_statistics)

        if free_kwh_statistics:
            metadata = {
                "has_sum": True,
                "name": "Free NZ Contact Energy Consumption",
                "source": DOMAIN,
                "statistic_id": f"{DOMAIN}:free_energy_consumption",
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                "mean_type": StatisticMeanType.NONE,
                "unit_class": "energy",
            }
            async_add_external_statistics(self.hass, metadata, free_kwh_statistics)

    def _mark_history_imported(self) -> None:
        """Persist history_imported so restarts skip full backfill."""
        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})
        if store.get("history_imported"):
            return

        store["history_imported"] = True
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, "history_imported": True}
            )
        _LOGGER.info("Historical import marked complete for %s", self._entry_id)

    # ------------------------------------------------------------------
    # Background one-time history import
    # ------------------------------------------------------------------

    async def _async_import_history(self) -> None:
        """Full backfill in the background — does not block setup/UI."""
        _LOGGER.info("Background historical import started")
        try:
            kwh_statistics, free_kwh_statistics = await self.hass.async_add_executor_job(
                self._fetch_data_and_process,
                True,  # full_history
            )
        except Exception:
            _LOGGER.exception("Background historical import failed")
            return

        if kwh_statistics is None:
            _LOGGER.error("Historical import aborted (login failed)")
            return

        self._write_statistics(kwh_statistics or [], free_kwh_statistics or [])
        self._mark_history_imported()
        _LOGGER.info("Background historical import finished")
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Data Fix import
    # ------------------------------------------------------------------

    async def async_import_date_range(
        self,
        start_date: date,
        end_date: date | None = None,
    ) -> None:
        """Fetch and insert statistics for an explicit date range."""
        end = end_date or start_date
        if end < start_date:
            raise ValueError("end_date must be >= start_date")

        _LOGGER.info("Manual import requested: %s → %s", start_date, end)

        kwh_seed = await self._async_get_last_sum(STATISTIC_ID, start_date)
        free_seed = await self._async_get_last_sum(FREE_STATISTIC_ID, start_date)

        kwh_statistics, free_kwh_statistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process,
            False,  # full_history (ignored when dates given)
            start_date,
            end,
            kwh_seed,
            free_seed,
        )

        if kwh_statistics is None:
            _LOGGER.error("Manual import failed (login or API error)")
            return

        self._write_statistics(kwh_statistics or [], free_kwh_statistics or [])
        self.async_write_ha_state()
        
        _LOGGER.info("Manual import finished: %s → %s", start_date, end)

    # ------------------------------------------------------------------
    # Normal periodic update (lag window only)
    # ------------------------------------------------------------------

    async def async_update(self) -> None:
        """Short update: last delta_no_data_days only."""
        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})

        if not store.get("history_imported"):
            _LOGGER.debug("Skipping poll; historical import still pending/running")
            return

        kwh_statistics, free_kwh_statistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process,
            False,  # full_history
        )

        if kwh_statistics is None:
            return

        self._write_statistics(kwh_statistics or [], free_kwh_statistics or [])

    # ------------------------------------------------------------------
    # Read last known sum for a statistic_id (used for data fix imports)
    # ------------------------------------------------------------------

    async def _async_get_last_sum(self, statistic_id: str, before: date) -> float:
        """Return the last known sum for statistic_id before the given date."""
        # Look at the hour just before the start of the requested range
        end = dt_util.start_of_local_day(before)  # midnight of start_date

        stats = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,  # only need the latest one
            statistic_id,
            True,  # convert units
            {"sum"},
        )

        if not stats or statistic_id not in stats or not stats[statistic_id]:
            return 0.0

        last = stats[statistic_id][-1]
        # Optional: only use it if it is really before our start
        return float(last.get("sum") or 0.0)