"""The NZ Contact Energy integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry  # noqa: TC002
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant  # noqa: TC002

from .const import CONF_DEBUG, DEFAULT_DEBUG

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up NZ Contact Energy from a config entry."""
    # Enable debug logging if the option is turned on
    if entry.options.get(CONF_DEBUG, DEFAULT_DEBUG):
        logging.getLogger("custom_components.nz_contact_energy").setLevel(logging.DEBUG)
        _LOGGER.debug("Debug logging enabled for NZ Contact Energy")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the integration when options are changed
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
