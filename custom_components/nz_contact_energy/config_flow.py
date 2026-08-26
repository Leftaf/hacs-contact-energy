"""Adds config flow for Contact Energy."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import selector

from .api import ContactEnergyApi
from .const import (
    CONF_ACCOUNT_ID,
    CONF_REFRESH_INTERVAL,
    CONF_USAGE_DAYS,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_USAGE_DAYS,
    DOMAIN,
    LOGGER,
)


class ContactEnergyFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Contact Energy."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle a flow initialized by the user."""
        _errors = {}
        if user_input is not None:
            # Validate credentials against the API in executor pool
            api = ContactEnergyApi(
                user_input[CONF_EMAIL], 
                user_input[CONF_PASSWORD]
            )
            success = await self.hass.async_add_executor_job(api.login)

            if success:
                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )
            _errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
                    ),
                    vol.Required(CONF_PASSWORD): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(CONF_ACCOUNT_ID, default=""): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                    ),
                    vol.Optional(CONF_USAGE_DAYS, default=DEFAULT_USAGE_DAYS): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=30, mode=selector.NumberSelectorMode.BOX)
                    ),
                    vol.Optional(CONF_REFRESH_INTERVAL, default=DEFAULT_REFRESH_INTERVAL): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=168, mode=selector.NumberSelectorMode.BOX)
                    ),
                }
            ),
            errors=_errors,
        )