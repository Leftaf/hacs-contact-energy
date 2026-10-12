"""Config flow for NZ Contact Energy integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .api import ContactEnergyApi
from .const import (
    CONF_ACCOUNT_ID,
    CONF_DEBUG,
    CONF_USAGE_DAYS,
    CONF_REFRESH_INTERVAL,
    DEFAULT_DEBUG,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_USAGE_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT_ID): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_USAGE_DAYS, default=DEFAULT_USAGE_DAYS): cv.positive_int,
        vol.Optional(
            CONF_REFRESH_INTERVAL, default=DEFAULT_REFRESH_INTERVAL
        ): cv.positive_int,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    if not data[CONF_ACCOUNT_ID] or not data[CONF_PASSWORD]:
        raise InvalidAuthError

    api = ContactEnergyApi(
        email=data[CONF_ACCOUNT_ID],
        password=data[CONF_PASSWORD],
    )

    success = await hass.async_add_executor_job(api.login)

    if not success:
        raise InvalidAuthError

    return {"title": f"Contact Energy ({data[CONF_ACCOUNT_ID]})"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for NZ Contact Energy."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step where the user enters credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for NZ Contact Energy."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Prefer existing options, fall back to original config data
        options = self.config_entry.options
        data = self.config_entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_USAGE_DAYS,
                        default=options.get(
                            CONF_USAGE_DAYS,
                            data.get(CONF_USAGE_DAYS, DEFAULT_USAGE_DAYS),
                        ),
                    ): cv.positive_int,
                    vol.Optional(
                        CONF_REFRESH_INTERVAL,
                        default=options.get(
                            CONF_REFRESH_INTERVAL,
                            data.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL),
                        ),
                    ): cv.positive_int,
                    vol.Optional(
                        CONF_DEBUG,
                        default=options.get(CONF_DEBUG, DEFAULT_DEBUG),
                    ): bool,
                }
            ),
        )


class InvalidAuthError(HomeAssistantError):
    """Error to indicate there is invalid auth."""
