"""NZ Contact Energy sensors.

Fetches hourly electricity usage from Contact Energy (NZ) and writes it into
Home Assistant long-term statistics so the Energy Dashboard can display it.

Key design points
-----------------
* Statistics use a *running sum* (cumulative kWh).  The Energy Dashboard
  shows the *delta* between consecutive hours, so the sum must stay continuous
  across day boundaries.  If you rewrite day N with a new sum, day N+1 must
  also be rewritten (or adjusted), otherwise a large positive/negative spike
  appears at the boundary.
* A one-time full history import runs on first setup.
* Later updates only re-fetch a short "lag window" (Contact data often arrives
  1–2 days late).
* A service ``nz_contact_energy.import_usage`` lets the user re-import an
  explicit date range (for repairs).  When only a start date is given the
  import is automatically extended through yesterday so the running sum stays
  continuous with everything after it.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_PASSWORD, UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.recorder import get_instance
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

# External statistic IDs (colon, not dot — required for external statistics)
STATISTIC_ID = f"{DOMAIN}:energy_consumption"
FREE_STATISTIC_ID = f"{DOMAIN}:free_energy_consumption"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Contact Energy sensor for a config entry.

    Setup returns immediately.  Historical data is loaded in the background
    so the UI is never blocked.
    """
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

    # update_before_add=False → entity is added at once; data loads later
    async_add_entities([sensor], update_before_add=False)

    # Store a reference so the import_usage service can find this sensor
    hass.data[DOMAIN][entry.entry_id]["sensor"] = sensor


class ContactEnergyUsageSensor(SensorEntity):
    """Contact Energy usage → external statistics for the Energy dashboard.

    The entity state is the latest cumulative kWh.  The real value for the
    Energy Dashboard lives in the recorder statistics tables, written by
    ``_write_statistics``.
    """

    _attr_should_poll = False  # we schedule our own interval updates

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
        """Initialise the sensor (no I/O here)."""
        self._attr_name = name
        self._api = api
        self._first_day_history = first_day_history
        self._delta_no_data_days = max(1, int(delta_no_data_days))
        self._refresh_hours = max(1, int(refresh_hours))
        self._entry_id = entry_id
        self._import_task = None
        self._unsub_interval = None

        # Shown under Settings → Devices & services as a "Service" device
        # (same pattern as Met.no).  contract_id may still be None until
        # the first successful login.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=f"Account {self._api._account_id}",
            manufacturer="Home Assistant Community",
            model=f"Contract {self._api._contract_id}",
            serial_number=str(self._api._contract_id),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://github.com/Leftaf/hacs-contact-energy",
            sw_version="1.0.0",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Extra attributes shown on the entity card."""
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
        """Called when the entity is added to Home Assistant.

        * First run  → schedule a full historical back-fill in the background.
        * Later runs → only a short lag-window refresh.
        * Always     → start the periodic update timer.
        """
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
            # History already done — just refresh the recent lag window
            self.hass.async_create_task(self.async_update_ha_state(force_refresh=True))

        # Fire async_update once every refresh_hours (not on HA's 30 s poll)
        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._async_interval_update,
            timedelta(hours=self._refresh_hours),
        )

    async def _async_interval_update(self, _now=None) -> None:
        """Timer callback — trigger a real data fetch."""
        await self.async_update_ha_state(force_refresh=True)

    async def async_will_remove_from_hass(self) -> None:
        """Clean up timers and API session when the entity is removed."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

        if self._import_task is not None and not self._import_task.done():
            self._import_task.cancel()
            self._import_task = None

        if hasattr(self._api, "close"):
            self._api.close()  # pyright: ignore[reportAttributeAccessIssue]

    # ------------------------------------------------------------------
    # Date-range helpers
    # ------------------------------------------------------------------

    def _date_range(self, *, full_history: bool = False) -> tuple[date, date]:
        """Return the inclusive date range to fetch, ending yesterday.

        * full_history=True  → from configured first_day_history
        * full_history=False → only the recent lag window (delta_no_data_days)
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
    # Blocking fetch (must run in an executor — uses requests + sleep)
    # ------------------------------------------------------------------

    def _fetch_data_and_process(
        self,
        full_history: bool = False,
        start_date: date | None = None,
        end_date: date | None = None,
        kwh_seed: float = 0.0,
        free_seed: float = 0.0,
    ) -> tuple[list[StatisticData] | None, list[StatisticData] | None]:
        """Fetch usage from Contact and build StatisticData points.

        This method is **blocking** (HTTP + sleep).  Always call it via
        ``hass.async_add_executor_job``.

        Parameters
        ----------
        full_history
            When no explicit dates are given, fetch from first_day_history.
        start_date / end_date
            Optional explicit range (used by the import_usage service).
        kwh_seed / free_seed
            Starting values for the running sums.  Must equal the last known
            sum *before* start_date so the Energy Dashboard deltas stay
            continuous.  Pass 0.0 for a brand-new statistic.
        """
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

        # Ensure we have a valid session
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
                # Contact's unofficial API is rate-sensitive
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
                        # Contact returns local timestamps as ISO strings
                        dt = datetime.fromisoformat(point["date"])

                        # offpeakValue == "0.00" → normal tariff
                        # anything else          → free / controlled period
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
    # Write statistics into the recorder (main thread / event loop)
    # ------------------------------------------------------------------

    def _write_statistics(
        self,
        kwh_statistics: list[StatisticData],
        free_kwh_statistics: list[StatisticData],
    ) -> None:
        """Push external statistics into the recorder.

        ``async_add_external_statistics`` replaces any existing points that
        share the same timestamp, so re-importing a range is safe.
        """
        if kwh_statistics:
            metadata: StatisticMetaData = {
                "has_sum": True,
                "name": "NZ Contact Energy Consumption",
                "source": DOMAIN,
                "statistic_id": STATISTIC_ID,
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
                "statistic_id": FREE_STATISTIC_ID,
                "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                "mean_type": StatisticMeanType.NONE,
                "unit_class": "energy",
            }
            async_add_external_statistics(self.hass, metadata, free_kwh_statistics)

    def _mark_history_imported(self) -> None:
        """Persist the history_imported flag so restarts skip the full back-fill."""
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
    # One-time full history import (background)
    # ------------------------------------------------------------------

    async def _async_import_history(self) -> None:
        """Full back-fill from first_day_history — does not block setup/UI."""
        _LOGGER.info("Background historical import started")
        try:
            (
                kwh_statistics,
                free_kwh_statistics,
            ) = await self.hass.async_add_executor_job(
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
        # Data is already in the statistics DB — just refresh the entity state
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Manual / repair import (called by the import_usage service)
    # ------------------------------------------------------------------

    async def async_import_date_range(
        self,
        start_date: date,
        end_date: date | None = None,
    ) -> None:
        """Fetch and insert statistics for an explicit date range.

        Called by the ``nz_contact_energy.import_usage`` service.

        Continuity rule
        ---------------
        Statistics use a cumulative sum.  If you rewrite day N, every day
        *after* N must also be rewritten (or adjusted), otherwise a large
        spike appears at the N / N+1 boundary.

        Therefore:
        * When only ``start_date`` is given we automatically extend the
          import through *yesterday*.
        * When both dates are given we import exactly that range — the
          caller is responsible for continuity.
        """
        if end_date is None:
            # Single day (or open-ended) request → extend through yesterday
            # so the running sum stays continuous with everything after it.
            end = dt_util.now().date() - timedelta(days=1)
            _LOGGER.info(
                "No end_date given – extending import from %s through %s "
                "to keep the running sum continuous",
                start_date,
                end,
            )
        else:
            end = end_date

        if end < start_date:
            raise ValueError("end_date must be >= start_date")

        _LOGGER.info("Manual import requested: %s → %s", start_date, end)

        # Seed the running sums from the last hour *before* start_date.
        # Without this, the first hour of the range would start near 0 and
        # the Energy Dashboard would show a large negative spike.
        kwh_seed = await self._async_get_last_sum(STATISTIC_ID, start_date)
        free_seed = await self._async_get_last_sum(FREE_STATISTIC_ID, start_date)

        kwh_statistics, free_kwh_statistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process,
            False,  # full_history (ignored when dates are given)
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
        """Short update: re-fetch the recent lag window only.

        Skipped while the one-time historical import is still running.
        Seeds the running sum from the hour before the lag window so
        normal polls never introduce a discontinuity.
        """
        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})

        if not store.get("history_imported"):
            _LOGGER.debug("Skipping poll; historical import still pending/running")
            return

        start, end = self._date_range(full_history=False)
        kwh_seed = await self._async_get_last_sum(STATISTIC_ID, start)
        free_seed = await self._async_get_last_sum(FREE_STATISTIC_ID, start)

        kwh_statistics, free_kwh_statistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process,
            False,
            start,
            end,
            kwh_seed,
            free_seed,
        )

        if kwh_statistics is None:
            return

        self._write_statistics(kwh_statistics or [], free_kwh_statistics or [])

    # ------------------------------------------------------------------
    # Seed helper — last known sum before a given date
    # ------------------------------------------------------------------

    async def _async_get_last_sum(self, statistic_id: str, before: date) -> float:
        """Return the cumulative sum of the last statistic hour before *before*.

        Used to seed the running sum so a re-import does not create a
        negative spike at the start of the range.

        Queries a short window ending at local midnight of *before* and
        takes the last point found.  Returns 0.0 when nothing exists yet
        (brand-new statistic).
        """
        start_of_day = dt_util.start_of_local_day(before)
        # Look a few days back so we still find a point if a day is missing
        window_start = start_of_day - timedelta(days=3)

        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            window_start,
            start_of_day,  # exclusive end = midnight of the day we import
            {statistic_id},
            "hour",
            None,  # units
            {"sum"},
        )

        if not stats or statistic_id not in stats or not stats[statistic_id]:
            _LOGGER.warning(
                "No prior statistics for %s before %s – seeding with 0",
                statistic_id,
                before,
            )
            return 0.0

        last = stats[statistic_id][-1]  # most recent hour before start_of_day
        seed = float(last.get("sum") or 0.0)
        _LOGGER.info(
            "Seed for %s before %s = %.3f (from %s)",
            statistic_id,
            before,
            seed,
            last.get("start"),
        )
        return seed
