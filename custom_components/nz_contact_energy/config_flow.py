"""Config flow for NZ Contact Energy integration."""

from __future__ import annotations

import logging
from typing import Any

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .api import ContactEnergyApi
from .const import (
    CONF_ACCOUNT_ID,
    CONF_DELTA_NO_DATA_DAYS,
    CONF_FIRST_DAY_HISTORY,
    CONF_REFRESH_INTERVAL,
    DEFAULT_DELTA_NO_DATA_DAYS,
    DEFAULT_REFRESH_INTERVAL,
    DOMAIN,
    default_first_day_history,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT_ID): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(
            CONF_FIRST_DAY_HISTORY,
            default=default_first_day_history(),
        ): str,  # "YYYY-MM-DD"
        vol.Optional(
            CONF_DELTA_NO_DATA_DAYS,
            default=DEFAULT_DELTA_NO_DATA_DAYS
        ): cv.positive_int,
        vol.Optional(
            CONF_REFRESH_INTERVAL, default=DEFAULT_REFRESH_INTERVAL
        ): cv.positive_int,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    if not data.get(CONF_ACCOUNT_ID) or not data.get(CONF_PASSWORD):
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
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options for NZ Contact Energy."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Prefer current options, fall back to original config data
        options = self.config_entry.options
        data = self.config_entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                vol.Optional(
                    CONF_DELTA_NO_DATA_DAYS,
                    default=options.get(
                        CONF_DELTA_NO_DATA_DAYS,
                        data.get(
                            CONF_DELTA_NO_DATA_DAYS, DEFAULT_DELTA_NO_DATA_DAYS
                        ),
                    ),
                ): cv.positive_int,
                vol.Optional(
                    CONF_REFRESH_INTERVAL,
                    default=options.get(
                        CONF_REFRESH_INTERVAL,
                        data.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL),
                    ),
                ): cv.positive_int,
                }
            ),
        )


class InvalidAuthError(HomeAssistantError):
    """Error to indicate there is invalid auth."""
