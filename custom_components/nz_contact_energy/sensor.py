"""NZ Contact Energy sensors.

Fetches hourly electricity usage from Contact Energy (NZ) and writes it into
Home Assistant long-term statistics so the Energy Dashboard can display it.

Key design points
-----------------
* Statistics use a *running sum* (cumulative kWh).  The Energy Dashboard
  shows the *delta* between consecutive hours, so the sum must stay continuous
  across day boundaries.
* On API errors (e.g. HTTP 502) the day loop **stops** and nothing is written
  for that batch — partial imports would poison later sums.
* Every write path that can change an end-of-day sum:
  - **Before:** seed the running sum from the last hour prior to the range.
  - **After:** shift all later hours by (new_end_sum - old_end_sum) so the
    next day stays continuous without re-fetching Contact.
* ``recalculate_sums`` only rebuilds deltas inside a chosen range (no API).
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from pytz import UTC

try:
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
except ImportError:  # pragma: no cover - older / split package layouts
    from homeassistant.components.recorder.models.statistics import (  # type: ignore[no-redef]
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)

try:
    from homeassistant.components.recorder.statistics import async_import_statistics
except ImportError:  # pragma: no cover
    async_import_statistics = None  # type: ignore[assignment]
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_PASSWORD, UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .api import ContactEnergyApi
from .const import (
    CONF_ACCOUNT_ID,
    CONF_DELTA_NO_DATA_DAYS,
    CONF_FIRST_DAY_HISTORY,
    CONF_REFRESH_INTERVAL,
    CONF_REFRESH_START_TIME,
    DEFAULT_DELTA_NO_DATA_DAYS,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_REFRESH_START_TIME,
    DOMAIN,
    SENSOR_COST_NAME,
    SENSOR_LAST_API_NAME,
    SENSOR_USAGE_NAME,
    VERSION,
    default_first_day_history,
    parse_refresh_start_time,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)


def _as_float(value: Any, default: float = 0.0) -> float:
    """Safely coerce sensor/state values to float (Pylance-friendly)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


STATISTIC_ID = f"{DOMAIN}:energy_consumption"
FREE_STATISTIC_ID = f"{DOMAIN}:free_energy_consumption"
COST_STATISTIC_ID = f"{DOMAIN}:energy_cost"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Contact Energy sensors for a config entry."""
    config = {**entry.data, **entry.options}

    api = ContactEnergyApi(config[CONF_ACCOUNT_ID], config[CONF_PASSWORD])

    usage = ContactEnergyUsageSensor(
        api=api,
        first_day_history=config.get(
            CONF_FIRST_DAY_HISTORY, default_first_day_history()
        ),
        delta_no_data_days=config.get(
            CONF_DELTA_NO_DATA_DAYS, DEFAULT_DELTA_NO_DATA_DAYS
        ),
        refresh_hours=config.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL),
        refresh_start_time=parse_refresh_start_time(
            config.get(CONF_REFRESH_START_TIME, DEFAULT_REFRESH_START_TIME)
        ),
        entry_id=entry.entry_id,
    )
    cost = ContactEnergyCostSensor(api=api, entry_id=entry.entry_id)
    last_api = ContactEnergyLastApiSensor(api=api, entry_id=entry.entry_id)
    usage.attach_cost_sensor(cost)
    usage.attach_last_api_sensor(last_api)

    async_add_entities([usage, cost, last_api], update_before_add=False)
    hass.data[DOMAIN][entry.entry_id]["sensor"] = usage
    hass.data[DOMAIN][entry.entry_id]["cost_sensor"] = cost
    hass.data[DOMAIN][entry.entry_id]["last_api_sensor"] = last_api


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
    _attr_name = SENSOR_USAGE_NAME

    def __init__(
        self,
        api: ContactEnergyApi,
        first_day_history: str,
        delta_no_data_days: int,
        refresh_hours: int,
        refresh_start_time,
        entry_id: str,
    ) -> None:
        """Initialise the sensor (no I/O here)."""
        self._api = api
        self._first_day_history = first_day_history
        self._delta_no_data_days = max(1, int(delta_no_data_days))
        self._refresh_hours = max(1, int(refresh_hours))
        self._refresh_start_time = parse_refresh_start_time(refresh_start_time)
        self._entry_id = entry_id
        self._import_task = None
        self._unsub_interval = None
        self._cost_sensor: ContactEnergyCostSensor | None = None
        self._last_api_sensor: ContactEnergyLastApiSensor | None = None
        self._pending_hour_costs: dict[str, float] = {}
        self._pending_cost_statistics: list[StatisticData] = []
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=f"Account {self._api.account_id}",
            manufacturer="Home Assistant Community",
            model=f"Contract {self._api.contract_id}",
            serial_number=str(self._api.contract_id),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://github.com/Leftaf/hacs-contact-energy",
            sw_version=VERSION,
        )

    def attach_cost_sensor(self, cost_sensor: ContactEnergyCostSensor) -> None:
        """Link the monetary cost sensor updated during usage fetches."""
        self._cost_sensor = cost_sensor

    def attach_last_api_sensor(self, last_api: ContactEnergyLastApiSensor) -> None:
        """Link the last-API-success timestamp sensor."""
        self._last_api_sensor = last_api

    def _notify_api_success(self, detail: str = "") -> None:
        """Record successful Contact API activity on the timestamp sensor."""
        if self._last_api_sensor is not None:
            self._last_api_sensor.set_success(detail)

    def _notify_api_error(
        self, detail: str = "", status: str | int | None = None
    ) -> None:
        """Record failed Contact API activity (status OK/code in entity state)."""
        if self._last_api_sensor is not None:
            self._last_api_sensor.set_error(detail, status=status)

    def _merge_hour_costs_and_update_sensor(self) -> None:
        """Merge per-hour NZD costs from the last fetch; persist; update cost entity.

        Keys are hour timestamps (ISO). Re-import replaces that hour (no double-count).
        Hour-level storage supports peak / off-peak / night tariffs.
        """
        if not self._pending_hour_costs:
            return

        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})
        cost_by_hour: dict[str, float] = store.setdefault("cost_by_hour", {})
        if not cost_by_hour and store.get("cost_by_day"):
            _LOGGER.info(
                "Migrating cost storage from per-day to per-hour "
                "(legacy day totals discarded; re-import history to rebuild)"
            )
            store.pop("cost_by_day", None)

        cost_by_hour.update(self._pending_hour_costs)
        self._pending_hour_costs = {}
        map_total = round(sum(float(v) for v in cost_by_hour.values()), 2)
        # Prefer the higher of hour-map sum and current entity value so a
        # partial lag-window map never pulls the cost entity down and create
        # a huge negative Energy day on "no data" / update day.
        current = 0.0
        if self._cost_sensor is not None:
            current = _as_float(self._cost_sensor.native_value, 0.0)
        total = round(max(map_total, current), 2)
        if self._cost_sensor is not None:
            self._cost_sensor.set_total(total)

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is not None:
            new_data = {**entry.data, "cost_by_hour": cost_by_hour}
            new_data.pop("cost_by_day", None)
            self.hass.config_entries.async_update_entry(entry, data=new_data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Extra attributes shown on the entity card."""
        return {
            "account": self._api.account_id,
            "contract_id": self._api.contract_id,
            "first_day_history": self._first_day_history,
            "delta_no_data_days": self._delta_no_data_days,
            "refresh_interval_hours": self._refresh_hours,
            "refresh_start_time": self._refresh_start_time.isoformat(
                timespec="seconds"
            ),
            "refresh_at_local_hours": self._refresh_hours_of_day(),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Entity added — start history import or lag-window refresh."""
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
            self.hass.async_create_task(self.async_update_ha_state(force_refresh=True))

        # Poll at refresh_start_time local, then every refresh_interval hours.
        # Example: 03:00 + 24h → 03:00 only; 03:00 + 12h → 03:00 and 15:00.
        hours = self._refresh_hours_of_day()
        minute = self._refresh_start_time.minute
        second = self._refresh_start_time.second
        _LOGGER.info(
            "Scheduling Contact lag-window at local %s (hours=%s, interval=%sh)",
            self._refresh_start_time.isoformat(timespec="seconds"),
            hours,
            self._refresh_hours,
        )
        self._unsub_interval = async_track_time_change(
            self.hass,
            self._async_interval_update,
            hour=hours,
            minute=minute,
            second=second,
        )

    @staticmethod
    def _hours_anchored(interval_hours: int, start_hour: int) -> list[int]:
        """Return local hours of day for polls: start_hour, +interval, within 24h."""
        interval = max(1, min(24, int(interval_hours)))
        start = int(start_hour) % 24
        hours: list[int] = []
        hour = start
        for _ in range(24):
            if hour not in hours:
                hours.append(hour)
            hour = (hour + interval) % 24
            if hour == start:
                break
        return sorted(hours)

    def _refresh_hours_of_day(self) -> list[int]:
        """Local clock hours when the lag-window fetch should run."""
        return self._hours_anchored(
            self._refresh_hours,
            self._refresh_start_time.hour,
        )

    async def _async_interval_update(self, _now=None) -> None:
        """Clock callback (refresh_start_time + interval) — real data fetch."""
        _LOGGER.debug("Scheduled Contact refresh fired")
        await self.async_update_ha_state(force_refresh=True)

    async def async_will_remove_from_hass(self) -> None:
        """Clean up timers and API session."""
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
        """Return inclusive date range ending yesterday."""
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
    # Blocking fetch — STOP on API error so sums stay continuous
    # ------------------------------------------------------------------

    def _fetch_data_and_process(
        self,
        full_history: bool = False,
        start_date: date | None = None,
        end_date: date | None = None,
        kwh_seed: float = 0.0,
        free_seed: float = 0.0,
        cost_seed: float = 0.0,
    ) -> tuple[list[StatisticData] | None, list[StatisticData] | None]:
        """Fetch usage and build StatisticData points.

        Blocking — call via ``hass.async_add_executor_job``.

        Returns
        -------
        (kwh_list, free_list)
            On success (may be empty lists if no points).
        (None, None)
            On login failure **or** any day-level API error.  Callers must
            **not** write statistics in that case.

        """
        if start_date is not None:
            start = start_date
            end = end_date or start_date
        else:
            start, end = self._date_range(full_history=full_history)

        _LOGGER.debug(
            "token=%s contract=%s account=%s range=%s → %s (full_history=%s)",
            self._api.has_session,
            self._api.contract_id,
            self._api.account_id,
            start,
            end,
            full_history,
        )

        # Always refresh login before a multi-day fetch. Stale tokens often
        # still look "valid" (has_session True) but Contact returns 502/401.
        _LOGGER.debug("Refreshing Contact session before usage fetch")
        if not self._api.login():
            _LOGGER.error("Failed to login - check credentials")
            return None, None

        kwh_statistics: list[StatisticData] = []
        free_kwh_statistics: list[StatisticData] = []
        cost_statistics: list[StatisticData] = []
        kwh_running_sum = kwh_seed
        free_kwh_running_sum = free_seed
        cost_running_sum = cost_seed
        # Per-hour NZD (dollarValue) — peak/off-peak friendly
        hour_costs: dict[str, float] = {}

        _LOGGER.info("Starting usage fetch from %s to %s", start, end)

        day = start
        while day <= end:
            try:
                # get_usage returns:
                #   list  → success (may be empty = no data for that day)
                #   None  → HTTP/API error (502, auth, network, …)
                response = self._api.get_usage(day.year, day.month, day.day)
                time.sleep(1.5)

                if response is None:
                    # Hard failure — abort so we never write a broken sum chain
                    _LOGGER.error(
                        "API error for %s — aborting fetch. "
                        "No statistics will be written for this run. "
                        "Retry import_usage later when Contact is available.",
                        day,
                    )
                    return None, None

                if not isinstance(response, list):
                    _LOGGER.error(
                        "Unexpected response type for %s: %s — aborting fetch",
                        day,
                        type(response).__name__,
                    )
                    return None, None

                if not response:
                    _LOGGER.warning("No usable data for %s (empty response)", day)
                    day += timedelta(days=1)
                    continue

                _LOGGER.info("Day %s → received %d points", day, len(response))

                for point in response:
                    if not isinstance(point, dict) or not point.get("value"):
                        continue
                    try:
                        val = float(point["value"])
                        dt = datetime.fromisoformat(point["date"])
                        # Charged cost for this hour (NZD) — same timestamp as kWh
                        cost_val = float(point.get("dollarValue") or 0.0)
                        hour_key = dt.replace(
                            minute=0,
                            second=0,
                            microsecond=0,
                        ).isoformat()
                        hour_costs[hour_key] = cost_val
                        cost_running_sum += cost_val
                        cost_statistics.append(
                            StatisticData(start=dt, sum=cost_running_sum),
                        )

                        if point.get("offpeakValue") == "0.00":
                            kwh_running_sum += val
                            kwh_statistics.append(
                                StatisticData(start=dt, sum=kwh_running_sum),
                            )
                        else:
                            free_kwh_running_sum += val
                            free_kwh_statistics.append(
                                StatisticData(start=dt, sum=free_kwh_running_sum),
                            )
                    except (ValueError, TypeError, KeyError) as err:
                        _LOGGER.warning("Skipping bad data point on %s: %s", day, err)

            except Exception:
                _LOGGER.exception(
                    "Error processing day %s — aborting fetch so sums stay consistent",
                    day,
                )
                return None, None

            day += timedelta(days=1)

        self._pending_hour_costs = hour_costs
        self._pending_cost_statistics = cost_statistics
        range_cost = round(sum(hour_costs.values()), 2)
        _LOGGER.info(
            "Finished processing %s → %s: %d normal, %d free, range cost=%.2f NZD",
            start,
            end,
            len(kwh_statistics),
            len(free_kwh_statistics),
            range_cost,
        )
        self._attr_native_value = round(kwh_running_sum, 2)
        return kwh_statistics, free_kwh_statistics

    # ------------------------------------------------------------------
    # Write statistics
    # ------------------------------------------------------------------

    @staticmethod
    def _dedupe_statistics(
        stats: list[StatisticData],
    ) -> list[StatisticData]:
        """One row per hour start (last wins). Avoids UNIQUE on metadata_id,start_ts."""
        if not stats:
            return stats
        by_hour: dict[datetime, StatisticData] = {}
        for point in stats:
            if isinstance(point, dict):
                start = point.get("start")
                sum_v = point.get("sum")
            else:
                start = getattr(point, "start", None)
                sum_v = getattr(point, "sum", None)
            if start is None:
                continue
            if isinstance(start, (int, float)):
                start = datetime.fromtimestamp(start, tz=UTC)
            elif isinstance(start, str):
                try:
                    start = datetime.fromisoformat(start)
                except ValueError:
                    continue
            if not isinstance(start, datetime):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            else:
                start = start.astimezone(UTC)
            hour = start.replace(minute=0, second=0, microsecond=0)
            by_hour[hour] = StatisticData(start=hour, sum=float(sum_v or 0.0))
        return [by_hour[k] for k in sorted(by_hour)]

    def _write_statistics(
        self,
        kwh_statistics: list[StatisticData],
        free_kwh_statistics: list[StatisticData],
        cost_statistics: list[StatisticData] | None = None,
    ) -> None:
        """Avoid duplicate hours and push external statistics into the recorder.

        kwh_statistics = self._dedupe_statistics(kwh_statistics or [])
        free_kwh_statistics = self._dedupe_statistics(free_kwh_statistics or [])
        cost_statistics = self._dedupe_statistics(cost_statistics or [])

        Push external statistics into the recorder (energy + cost).
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

            # Mirror onto consumption entity LTS (Energy dashboard entity source)
            if self.entity_id and async_import_statistics is not None:
                entity_meta = cast(
                    "StatisticMetaData",
                    {
                        "has_sum": True,
                        "name": None,
                        "source": "recorder",
                        "statistic_id": self.entity_id,
                        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                        "mean_type": StatisticMeanType.NONE,
                        "unit_class": "energy",
                    },
                )
                try:
                    async_import_statistics(self.hass, entity_meta, kwh_statistics)
                except Exception:
                    _LOGGER.exception(
                        "Failed to import kWh statistics onto entity %s",
                        self.entity_id,
                    )

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

        # Hourly cost sums (same timestamps as kWh) so Energy days match
        if cost_statistics:
            metadata = {
                "has_sum": True,
                "name": "NZ Contact Energy Cost",
                "source": DOMAIN,
                "statistic_id": COST_STATISTIC_ID,
                "unit_of_measurement": "NZD",
                "mean_type": StatisticMeanType.NONE,
                "unit_class": None,
            }
            async_add_external_statistics(self.hass, metadata, cost_statistics)

            # Also import onto the cost *entity* statistic_id so the Energy
            # dashboard "entity tracking total costs" option sees the same hours
            if self._cost_sensor is not None and self._cost_sensor.entity_id:
                entity_meta = cast(
                    "StatisticMetaData",
                    {
                        "has_sum": True,
                        "name": None,
                        "source": "recorder",
                        "statistic_id": self._cost_sensor.entity_id,
                        "unit_of_measurement": "NZD",
                        "mean_type": StatisticMeanType.NONE,
                        "unit_class": None,
                    },
                )
                if async_import_statistics is not None:
                    try:
                        async_import_statistics(self.hass, entity_meta, cost_statistics)
                    except Exception:
                        _LOGGER.exception(
                            "Failed to import cost statistics for %s",
                            self._cost_sensor.entity_id,
                        )

    def _mark_history_imported(self) -> None:
        """Persist history_imported so restarts skip the full back-fill."""
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
    # One-time full history import
    # ------------------------------------------------------------------

    async def _async_import_history(self) -> None:
        """Full back-fill from first_day_history."""
        _LOGGER.info("Background historical import started")
        try:
            (
                kwh_statistics,
                free_kwh_statistics,
            ) = await self.hass.async_add_executor_job(
                self._fetch_data_and_process,
                True,
            )
        except Exception:
            _LOGGER.exception("Background historical import failed")
            return

        if kwh_statistics is None:
            _LOGGER.error(
                "Historical import aborted (login or API error). "
                "Will retry on next restart or via import_usage."
            )
            return

        self._write_statistics(
            kwh_statistics or [],
            free_kwh_statistics or [],
            self._pending_cost_statistics,
        )
        self._pending_cost_statistics = []
        self._mark_history_imported()
        self._merge_hour_costs_and_update_sensor()
        _LOGGER.info("Background historical import finished")
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Manual import (Contact API)
    # ------------------------------------------------------------------

    async def async_import_date_range(
        self,
        start_date: date,
        end_date: date | None = None,
    ) -> None:
        """Fetch and insert statistics for an explicit date range.

        * Only ``start_date`` → import **that single day**.
        * ``start_date`` + ``end_date`` → import exactly that range.

        Before/after continuity
        -----------------------
        * **Before:** running sum is seeded from the last hour prior to
          ``start_date``.
        * **After:** days after ``end`` are shifted by a constant offset
          (new end-of-range sum - old end-of-range sum) so hourly deltas on
          later days stay the same but align with the rewritten range.
          This is more reliable than rebuilding later days from deltas.

        On any API error the fetch aborts and **nothing is written**.
        """
        if end_date is None:
            end = start_date
            _LOGGER.info(
                "No end_date given - importing single day %s, "
                "then shifting later days to match the new sum",
                start_date,
            )
        else:
            end = end_date

        if end < start_date:
            raise ValueError("end_date must be >= start_date")

        _LOGGER.info("Manual import requested: %s → %s", start_date, end)
        await self._async_fetch_write_and_shift(start_date, end)
        _LOGGER.info("Manual import finished: %s → %s", start_date, end)

    # ------------------------------------------------------------------
    # Recalculate sums (no API) — fix spikes from bad partial imports
    # ------------------------------------------------------------------

    async def async_recalculate_sums(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> None:
        """Rebuild cumulative sums from existing recorder statistics.

        Does **not** call Contact.  Reads hourly points already stored, derives
        non-negative deltas between consecutive hours, and rewrites a continuous
        running sum for **kWh, free kWh, and cost**.  Use after partial imports
        or negative spikes.

        * **Before:** seed from the last hour prior to ``start_date``.
        * **After:** shift all later hours through yesterday by
          (new_end_sum - old_end_sum) so the day after the range stays continuous.

        Missing hours are not invented — only existing points are rewritten.
        """
        if start_date is None:
            start_date = date.fromisoformat(self._first_day_history)
        if end_date is None:
            end_date = dt_util.now().date() - timedelta(days=1)

        if end_date < start_date:
            raise ValueError("end_date must be >= start_date")

        _LOGGER.info("Recalculate sums requested: %s → %s", start_date, end_date)

        yesterday = dt_util.now().date() - timedelta(days=1)

        # (statistic_id, kind) — kind: "kwh" | "free" | "cost"
        series: list[tuple[str, str]] = [
            (STATISTIC_ID, "kwh"),
            (FREE_STATISTIC_ID, "free"),
            (COST_STATISTIC_ID, "cost"),
        ]
        # Also rebuild entity cost stats if Energy uses the sensor entity
        entity_id = self._cost_entity_statistic_id()
        if entity_id is not None:
            series.append((entity_id, "cost_entity"))

        for statistic_id, kind in series:
            if kind == "cost":
                old_end = await self._async_get_cost_sum_at_end_of_day(end_date)
            elif kind == "cost_entity":
                old_end = await self._async_get_sum_at_end_of_day(
                    statistic_id,
                    end_date,
                )
            else:
                old_end = await self._async_get_sum_at_end_of_day(
                    statistic_id,
                    end_date,
                )

            rebuilt = await self._async_rebuild_sums_for_id(
                statistic_id,
                start_date,
                end_date,
            )
            if not rebuilt:
                _LOGGER.info("No points to recalculate for %s", statistic_id)
                continue

            if kind == "free":
                self._write_statistics([], rebuilt)
            elif kind in ("cost", "cost_entity"):
                # Writes external cost + imports onto cost entity when applicable
                self._write_statistics([], [], rebuilt)
            else:
                self._write_statistics(rebuilt, [])

            new_end = self._statistic_sum(rebuilt[-1])
            offset = new_end - old_end
            _LOGGER.info(
                "Recalculated %d points for %s (end %.3f → %.3f, offset=%+.3f)",
                len(rebuilt),
                statistic_id,
                old_end,
                new_end,
                offset,
            )

            if yesterday > end_date and abs(offset) > 1e-6:
                _LOGGER.info(
                    "Shifting %s after %s through %s by %+.3f",
                    statistic_id,
                    end_date,
                    yesterday,
                    offset,
                )
                await self._async_shift_sums_after(
                    statistic_id, end_date, yesterday, offset
                )

        # Keep live cost sensor total aligned with hour map + stats
        store = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        cost_by_hour = store.get("cost_by_hour") or {}
        if cost_by_hour and self._cost_sensor is not None:
            self._cost_sensor.set_total(
                round(sum(float(v) for v in cost_by_hour.values()), 2)
            )

        self.async_write_ha_state()
        _LOGGER.info("Recalculate sums finished: %s → %s", start_date, end_date)

    async def _async_rebuild_sums_for_id(
        self,
        statistic_id: str,
        start_date: date,
        end_date: date,
    ) -> list[StatisticData]:
        """Load stats in range, rebuild continuous sum from deltas."""
        if statistic_id == COST_STATISTIC_ID or (
            self._cost_sensor is not None
            and statistic_id == self._cost_sensor.entity_id
        ):
            seed = await self._async_get_cost_seed(start_date)
        else:
            seed = await self._async_get_last_sum(statistic_id, start_date)

        window_start = dt_util.start_of_local_day(start_date)
        # exclusive end = midnight after end_date
        window_end = dt_util.start_of_local_day(end_date + timedelta(days=1))

        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            window_start,
            window_end,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )

        if not stats or statistic_id not in stats or not stats[statistic_id]:
            return []

        points = stats[statistic_id]
        rebuilt: list[StatisticData] = []
        running = seed
        prev_old_sum: float | None = None

        for point in points:
            start_raw = point.get("start")
            old_sum = float(point.get("sum") or 0.0)
            start_dt = self._parse_statistic_start(start_raw)
            if start_dt is None:
                continue

            if prev_old_sum is None:
                # Link first hour to the seed (last sum before this range).
                # Energy dashboard shows (this_sum - previous_sum) for the bar.
                delta = old_sum - seed
                if delta < 0:
                    # Backward jump relative to the day before.
                    # - Large drop + small absolute sum → series was reset near 0;
                    #   the stored first sum is the first hour's usage from that reset.
                    # - Otherwise use the magnitude of the negative gap so the hour
                    #   is not left at 0 (matches the size of the old negative bar).
                    drop = seed - old_sum
                    if old_sum >= 0 and drop > max(50.0, abs(seed) * 0.05):
                        delta = old_sum
                        _LOGGER.info(
                            "First point looks like a sum reset (seed=%.3f, "
                            "first=%.3f); using %.3f as first-hour usage",
                            seed,
                            old_sum,
                            delta,
                        )
                    else:
                        delta = drop
                        _LOGGER.info(
                            "First point below seed (seed=%.3f, first=%.3f); "
                            "using gap magnitude %.3f as first-hour usage",
                            seed,
                            old_sum,
                            delta,
                        )
                running = seed + delta
            else:
                delta = old_sum - prev_old_sum
                if delta < 0:
                    # Mid-series negative jump: use magnitude so the hour is not zeroed
                    delta = -delta
                    _LOGGER.debug(
                        "Negative mid-series delta at %s; using magnitude %.3f",
                        start_dt,
                        delta,
                    )
                running += delta

            prev_old_sum = old_sum
            rebuilt.append(StatisticData(start=start_dt, sum=running))

        return rebuilt

    @staticmethod
    def _parse_statistic_start(start_raw: Any) -> datetime | None:
        """Normalise statistic start to a timezone-aware datetime."""
        if start_raw is None:
            return None
        if isinstance(start_raw, datetime):
            if start_raw.tzinfo is None:
                return start_raw.replace(tzinfo=UTC)
            return start_raw
        if isinstance(start_raw, (int, float)):
            # Unix timestamp (seconds)
            return datetime.fromtimestamp(start_raw, tz=UTC)
        if isinstance(start_raw, str):
            try:
                return datetime.fromisoformat(start_raw)
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------
    # Normal periodic update
    # ------------------------------------------------------------------

    async def async_update(self) -> None:
        """Short update: lag window only.

        Seeds from the day before the window; after writing, shifts any days
        after the window (normally none, because the window ends yesterday).
        Aborts with no write on API error.
        """
        store = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})

        if not store.get("history_imported"):
            _LOGGER.debug("Skipping poll; historical import still pending/running")
            return

        start, end = self._date_range(full_history=False)
        await self._async_fetch_write_and_shift(start, end)

    # ------------------------------------------------------------------
    # Seed helper
    # ------------------------------------------------------------------

    async def _async_fetch_write_and_shift(
        self,
        start: date,
        end: date,
    ) -> bool:
        """Seed from day before, fetch ``start``…``end``, write, shift days after.

        Returns True if statistics were written, False on abort (API error).
        """
        # --- Before: seed from last hour prior to start ---
        kwh_seed = await self._async_get_last_sum(STATISTIC_ID, start)
        free_seed = await self._async_get_last_sum(FREE_STATISTIC_ID, start)
        cost_seed = await self._async_get_cost_seed(start)

        # Old end-of-range sums (before overwrite) for offset calculation
        old_kwh_end = await self._async_get_sum_at_end_of_day(STATISTIC_ID, end)
        old_free_end = await self._async_get_sum_at_end_of_day(FREE_STATISTIC_ID, end)
        old_cost_end = await self._async_get_cost_sum_at_end_of_day(end)

        kwh_statistics, free_kwh_statistics = await self.hass.async_add_executor_job(
            self._fetch_data_and_process,
            False,
            start,
            end,
            kwh_seed,
            free_seed,
            cost_seed,
        )

        if kwh_statistics is None:
            _LOGGER.error(
                "Fetch aborted for %s → %s (API error). No statistics written.",
                start,
                end,
            )
            return False

        cost_statistics = list(self._pending_cost_statistics)

        self._write_statistics(
            kwh_statistics or [],
            free_kwh_statistics or [],
            cost_statistics,
        )
        self._pending_cost_statistics = []
        self.async_write_ha_state()
        self._merge_hour_costs_and_update_sensor()
        # Align cost entity with last written cost running sum (if any)
        if cost_statistics and self._cost_sensor is not None:
            last_cost = self._statistic_sum(cost_statistics[-1])
            cur = _as_float(self._cost_sensor.native_value, 0.0)
            if last_cost > cur + 1e-6:
                self._cost_sensor.set_total(round(last_cost, 2))
        self._notify_api_success(f"{start} -> {end}")

        # --- After: shift later days by (new_end - old_end) ---
        # Use *in-memory* end sums from this fetch — not a recorder re-read.
        # Reading the DB immediately after async_add_external_statistics can
        # still return the *old* end sum, so cost_offset becomes 0 and later
        # days (e.g. June after a Mar - May import) stay unshifted → large
        # negative Energy day totals.
        new_kwh_end = (
            self._statistic_sum(kwh_statistics[-1]) if kwh_statistics else old_kwh_end
        )
        new_free_end = (
            self._statistic_sum(free_kwh_statistics[-1])
            if free_kwh_statistics
            else old_free_end
        )
        new_cost_end = (
            self._statistic_sum(cost_statistics[-1])
            if cost_statistics
            else old_cost_end
        )
        kwh_offset = new_kwh_end - old_kwh_end
        free_offset = new_free_end - old_free_end
        cost_offset = new_cost_end - old_cost_end
        _LOGGER.debug(
            "Import range %s → %s end sums: kwh %.3f→%.3f (offset %+.3f), "
            "cost %.3f→%.3f (offset %+.3f)",
            start,
            end,
            old_kwh_end,
            new_kwh_end,
            kwh_offset,
            old_cost_end,
            new_cost_end,
            cost_offset,
        )

        yesterday = dt_util.now().date() - timedelta(days=1)
        if yesterday > end and (
            abs(kwh_offset) > 1e-6 or abs(free_offset) > 1e-6 or abs(cost_offset) > 1e-6
        ):
            _LOGGER.info(
                "Shifting sums after %s through %s "
                "(kwh offset=%+.3f, free offset=%+.3f, cost offset=%+.3f)",
                end,
                yesterday,
                kwh_offset,
                free_offset,
                cost_offset,
            )
            if abs(kwh_offset) > 1e-6:
                await self._async_shift_sums_after(
                    STATISTIC_ID,
                    end,
                    yesterday,
                    kwh_offset,
                )
                # Entity LTS (Energy dashboard when source is the sensor)
                if self.entity_id:
                    await self._async_shift_sums_after(
                        self.entity_id,
                        end,
                        yesterday,
                        kwh_offset,
                    )
            if abs(free_offset) > 1e-6:
                await self._async_shift_sums_after(
                    FREE_STATISTIC_ID,
                    end,
                    yesterday,
                    free_offset,
                )
            if abs(cost_offset) > 1e-6:
                await self._async_shift_sums_after(
                    COST_STATISTIC_ID,
                    end,
                    yesterday,
                    cost_offset,
                )
                # Energy dashboard reads the cost *entity* statistics — keep in sync
                entity_id = self._cost_entity_statistic_id()
                if entity_id is not None:
                    await self._async_shift_sums_after(
                        entity_id,
                        end,
                        yesterday,
                        cost_offset,
                    )
            self.async_write_ha_state()

        return True

    @staticmethod
    def _statistic_sum(point: Any) -> float:
        """Read ``sum`` from a StatisticData dict or object."""
        if isinstance(point, dict):
            return float(point.get("sum") or 0.0)
        return float(getattr(point, "sum", 0.0) or 0.0)

    def _cost_entity_statistic_id(self) -> str | None:
        """Recorder statistic_id for the cost entity (Energy dashboard cost source)."""
        if self._cost_sensor is not None and self._cost_sensor.entity_id:
            return self._cost_sensor.entity_id
        return None

    def _cost_seed_from_hour_map(self, before: date) -> float:
        """Sum of persisted per-hour costs strictly before local midnight."""
        store = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        cost_by_hour = store.get("cost_by_hour") or {}
        if not cost_by_hour:
            return 0.0

        before_start = dt_util.start_of_local_day(before)
        total = 0.0
        for key, val in cost_by_hour.items():
            try:
                dt = datetime.fromisoformat(str(key))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=before_start.tzinfo)
                if dt < before_start:
                    total += float(val)
            except TypeError, ValueError:
                continue
        return total

    async def _async_get_cost_seed(self, before: date) -> float:
        """Seed cost running sum: external stats → entity stats → hour map → 0."""
        seed = await self._async_get_last_sum(COST_STATISTIC_ID, before)
        if abs(seed) > 1e-9:
            return seed

        entity_id = self._cost_entity_statistic_id()
        if entity_id is not None:
            seed = await self._async_get_last_sum(entity_id, before)
            if abs(seed) > 1e-9:
                _LOGGER.info(
                    "Cost seed from entity %s before %s = %.3f",
                    entity_id,
                    before,
                    seed,
                )
                return seed

        seed = self._cost_seed_from_hour_map(before)
        if abs(seed) > 1e-9:
            _LOGGER.info("Cost seed from cost_by_hour before %s = %.3f", before, seed)
        return seed

    async def _async_get_cost_sum_at_end_of_day(self, day: date) -> float:
        """End-of-day cost sum: prefer external, else entity."""
        end = await self._async_get_sum_at_end_of_day(COST_STATISTIC_ID, day)
        if abs(end) > 1e-9:
            return end
        entity_id = self._cost_entity_statistic_id()
        if entity_id is not None:
            return await self._async_get_sum_at_end_of_day(entity_id, day)
        return 0.0

    async def _async_get_last_sum(self, statistic_id: str, before: date) -> float:
        """Return cumulative sum of the last hour before local midnight of *before*."""
        start_of_day = dt_util.start_of_local_day(before)
        window_start = start_of_day - timedelta(days=3)

        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            window_start,
            start_of_day,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )

        if not stats or statistic_id not in stats or not stats[statistic_id]:
            _LOGGER.debug(
                "No prior statistics for %s before %s - seeding with 0",
                statistic_id,
                before,
            )
            return 0.0

        last = stats[statistic_id][-1]
        seed = float(last.get("sum") or 0.0)
        _LOGGER.info(
            "Seed for %s before %s = %.3f (from %s)",
            statistic_id,
            before,
            seed,
            last.get("start"),
        )
        return seed

    async def _async_get_sum_at_end_of_day(self, statistic_id: str, day: date) -> float:
        """Return cumulative sum at end of *day* (local).

        Prefer the last hour *on* that day. If Contact has not published the
        day yet (empty lag day), fall back to the last hour strictly before
        the next midnight — never return 0 when earlier history exists.
        Returning 0 for an empty day made import offsets wrong and produced
        large negative Energy cost on "no data" days.
        """
        day_start = dt_util.start_of_local_day(day)
        day_end = dt_util.start_of_local_day(day + timedelta(days=1))

        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            day_start,
            day_end,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )

        if stats and statistic_id in stats and stats[statistic_id]:
            return float(stats[statistic_id][-1].get("sum") or 0.0)

        # Empty day (portal lag): use last sum before end of this day
        lookback_start = day_start - timedelta(days=14)
        prior = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            lookback_start,
            day_end,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        if prior and statistic_id in prior and prior[statistic_id]:
            seed = float(prior[statistic_id][-1].get("sum") or 0.0)
            _LOGGER.debug(
                "No stats on %s for %s — using last prior sum %.3f",
                day,
                statistic_id,
                seed,
            )
            return seed
        return 0.0

    async def _async_shift_sums_after(
        self,
        statistic_id: str,
        after_day: date,
        end_date: date,
        offset: float,
    ) -> None:
        """Add *offset* to every hourly sum after *after_day* through *end_date*.

        Preserves hour-to-hour deltas on later days; only moves the baseline
        so they connect to a rewritten earlier range.
        """
        if abs(offset) < 1e-9:
            return

        window_start = dt_util.start_of_local_day(after_day + timedelta(days=1))
        window_end = dt_util.start_of_local_day(end_date + timedelta(days=1))

        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            window_start,
            window_end,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )

        if not stats or statistic_id not in stats or not stats[statistic_id]:
            _LOGGER.info(
                "No statistics after %s to shift for %s", after_day, statistic_id
            )
            return

        shifted: list[StatisticData] = []
        for point in stats[statistic_id]:
            start_dt = self._parse_statistic_start(point.get("start"))
            if start_dt is None:
                continue
            old_sum = float(point.get("sum") or 0.0)
            shifted.append(StatisticData(start=start_dt, sum=old_sum + offset))

        if not shifted:
            return

        if statistic_id == FREE_STATISTIC_ID:
            self._write_statistics([], shifted)
        elif statistic_id == COST_STATISTIC_ID:
            self._write_statistics([], [], shifted)
        elif (
            self._cost_sensor is not None
            and statistic_id == self._cost_sensor.entity_id
        ):
            # Entity-owned cost statistics (Energy "total costs" source)
            if async_import_statistics is not None:
                entity_meta = cast(
                    "StatisticMetaData",
                    {
                        "has_sum": True,
                        "name": None,
                        "source": "recorder",
                        "statistic_id": statistic_id,
                        "unit_of_measurement": "NZD",
                        "mean_type": StatisticMeanType.NONE,
                        "unit_class": None,
                    },
                )
                try:
                    async_import_statistics(self.hass, entity_meta, shifted)
                except Exception:
                    _LOGGER.exception(
                        "Failed to shift cost entity statistics for %s",
                        statistic_id,
                    )
            # Keep external cost series aligned too
            self._write_statistics([], [], shifted)
        else:
            self._write_statistics(shifted, [])

        _LOGGER.info(
            "Shifted %d points for %s after %s by %+.3f",
            len(shifted),
            statistic_id,
            after_day,
            offset,
        )


class ContactEnergyCostSensor(SensorEntity):
    """Running total of electricity cost (NZD) from Contact hourly dollarValue.

    Use in the Energy dashboard under
    **Grid consumption → Cost tracking → Use an entity tracking the total costs**.

    Monetary sensors must use state_class TOTAL (not TOTAL_INCREASING).
    """

    _attr_should_poll = False
    _attr_icon = "mdi:currency-usd"
    _attr_native_value = 0.0
    _attr_native_unit_of_measurement = "NZD"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_has_entity_name = True
    _attr_name = SENSOR_COST_NAME

    def __init__(self, api: ContactEnergyApi, entry_id: str) -> None:
        """Initialise the cost sensor (no I/O)."""
        self._api = api
        self._entry_id = entry_id
        self._attr_unique_id = f"{DOMAIN}_cost"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=f"Account {self._api.account_id}",
            manufacturer="Home Assistant Community",
            model=f"Contract {self._api.contract_id}",
            serial_number=str(self._api.contract_id),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://github.com/Leftaf/hacs-contact-energy",
            sw_version=VERSION,
        )

    async def async_added_to_hass(self) -> None:
        """Restore cumulative cost from persisted per-hour totals."""
        store = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        cost_by_hour = store.get("cost_by_hour") or {}
        if cost_by_hour:
            self._attr_native_value = round(
                sum(float(v) for v in cost_by_hour.values()), 2
            )

    def set_total(self, total: float) -> None:
        """Update the cumulative cost total and push state to Home Assistant."""
        self._attr_native_value = round(float(total), 2)
        if self.hass is not None:
            self.async_write_ha_state()


class ContactEnergyLastApiSensor(RestoreEntity, SensorEntity):
    """Last Contact API call status for Activity / diagnostics.

    State is a short status string (``OK`` or an error code such as ``502``),
    not a timestamp — so Activity shows useful transitions (e.g. OK → 502).
    The time of the last successful call is in attributes (and ``short``).
    """

    _attr_should_poll = False
    _attr_icon = "mdi:api"
    _attr_has_entity_name = True
    _attr_name = SENSOR_LAST_API_NAME
    _attr_native_value = "unknown"

    def __init__(self, api: ContactEnergyApi, entry_id: str) -> None:
        """Initialise the last-API status sensor."""
        self._api = api
        self._entry_id = entry_id
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self._last_detail: str = ""
        self._attr_unique_id = f"{DOMAIN}_last_api_call"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=f"Account {self._api.account_id}",
            manufacturer="Home Assistant Community",
            model=f"Contract {self._api.contract_id}",
            serial_number=str(self._api.contract_id),
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://github.com/Leftaf/hacs-contact-energy",
            sw_version=VERSION,
        )

    async def async_added_to_hass(self) -> None:
        """Restore last status and success time after restart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (None, "unknown", "unavailable"):
            return
        # Prefer restored status string (OK / 502 / …)
        self._attr_native_value = str(last.state)
        if last.attributes:
            self._last_detail = str(last.attributes.get("detail") or "")
            err = last.attributes.get("last_error")
            self._last_error = str(err) if err else None
            raw = last.attributes.get("last_success")
            if raw:
                parsed = dt_util.parse_datetime(str(raw))
                if parsed is not None:
                    self._last_success = dt_util.as_local(parsed)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose time, range detail, and last error text."""
        attrs: dict[str, Any] = {
            "status": self._attr_native_value,
        }
        if self._last_success is not None:
            local = dt_util.as_local(self._last_success)
            attrs["last_success"] = local.isoformat()
            attrs["short"] = local.strftime("%Y-%m-%d %H:%M")
        if self._last_detail:
            attrs["detail"] = self._last_detail
        if self._last_error:
            attrs["last_error"] = self._last_error
        return attrs

    def set_success(self, detail: str = "") -> None:
        """Mark a successful API fetch: state OK + update last_success time."""
        self._attr_native_value = "OK"
        self._last_success = dt_util.now()
        self._last_detail = detail
        self._last_error = None
        if self.hass is not None:
            self.async_write_ha_state()

    def set_error(
        self,
        detail: str = "",
        *,
        status: str | int | None = None,
    ) -> None:
        """Mark a failed API fetch; state becomes the status code or ``error``."""
        if status is not None and str(status).strip():
            self._attr_native_value = str(status).strip()
        else:
            self._attr_native_value = "error"
        self._last_error = detail or str(self._attr_native_value)
        self._last_detail = detail
        # Keep last_success as previous OK time
        if self.hass is not None:
            self.async_write_ha_state()
