"""Config flow for NZ Contact Energy integration."""

from __future__ import annotations  # noqa: I001

import logging
from typing import Any, TYPE_CHECKING

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

if TYPE_CHECKING:
    from homeassistant.data_entry_flow import FlowResult

from .api import ContactEnergyApi  # noqa: I001
from .const import (
    CONF_ACCOUNT_ID,
    CONF_USAGE_DAYS,
    CONF_REFRESH_INTERVAL,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_USAGE_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Define the form layout and types using Voluptuous
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

    if not data[CONF_ACCOUNT_ID] or not data[CONF_PASSWORD]:
        raise InvalidAuthError

    """Validate the user input allows us to connect."""
    api = ContactEnergyApi(
        email=data[CONF_ACCOUNT_ID],
        password=data[CONF_PASSWORD],
    )

    # Run blocking requests network call in an executor thread
    success = await hass.async_add_executor_job(api.login)

    if not success:
        raise InvalidAuthError

    # Return info that you want to store in the config entry title/data.
    return {"title": f"Contact Energy ({data[CONF_ACCOUNT_ID]})"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for NZ Contact Energy."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ):
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
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


class InvalidAuthError(HomeAssistantError):
    """Error to indicate there is invalid auth."""

    pass
