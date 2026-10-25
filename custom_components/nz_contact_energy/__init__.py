"""The NZ Contact Energy integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

if TYPE_CHECKING:
    from datetime import date

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SERVICE_IMPORT_USAGE = "import_usage"
SERVICE_RECALCULATE_SUMS = "recalculate_sums"

SERVICE_IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required("start_date"): cv.date,
        vol.Optional("end_date"): cv.date,
    },
)
SERVICE_RECALCULATE_SCHEMA = vol.Schema(
    {
        vol.Required("start_date"): cv.date,
        vol.Optional("end_date"): cv.date,
    },
)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NZ Contact Energy from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "history_imported": entry.data.get("history_imported", False),
        "sensor": None,
        "cost_sensor": None,
        "cost_by_hour": dict(entry.data.get("cost_by_hour") or {}),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register service once (guard against reloads)
    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_USAGE):

        async def async_import_usage(call: ServiceCall) -> None:
            start: date = call.data["start_date"]
            end: date | None = call.data.get("end_date")

            # Use the first (usually only) configured entry
            for entry_id, store in hass.data.get(DOMAIN, {}).items():
                sensor = store.get("sensor")
                if sensor is not None:
                    await sensor.async_import_date_range(start, end)
                    return

            _LOGGER.error("No Contact Energy sensor available for import_usage")

        async def async_recalculate_sums(call: ServiceCall) -> None:
            """Rebuild cumulative sums from existing statistics (no API calls)."""
            start: date | None = call.data.get("start_date")
            end: date | None = call.data.get("end_date")

            # Use the first (usually only) configured entry
            for _entry_id, store in hass.data.get(DOMAIN, {}).items():
                sensor = store.get("sensor")
                if sensor is not None:
                    await sensor.async_recalculate_sums(start, end)
                    return

            _LOGGER.error("No Contact Energy sensor available for recalculate_sums")

        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_USAGE,
            async_import_usage,
            schema=SERVICE_IMPORT_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_RECALCULATE_SUMS,
            async_recalculate_sums,
            schema=SERVICE_RECALCULATE_SCHEMA,
        )

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
