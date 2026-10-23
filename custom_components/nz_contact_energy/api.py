"""Contact Energy API client."""

from __future__ import annotations

import logging
from typing import Any
from pathlib import Path

import requests

_LOGGER = logging.getLogger(__name__)

DEFAULT_API_KEY = "z840P4lQCH9TqcjC9L2pP157DZcZJMcr5tVQCvyx"
BASE_URL = "https://api.contact-digital-prod.net"
REQUEST_TIMEOUT = 15


class ContactEnergyApi:
    """Minimal client for the unofficial Contact Energy backend API."""

    def __init__(
        self,
        email: str,
        password: str,
        api_key: str = DEFAULT_API_KEY,
    ) -> None:
        """Initialise the API client."""
        self._email = email
        self._password = password
        self._api_key = api_key
        self._url_base = BASE_URL

        self._api_token = ""
        self._api_session = ""
        self._account_id = ""
        self._contract_id = ""

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """Login, refresh session, and load account/contract. Returns True on success."""
        headers = {"x-api-key": self._api_key}
        data = {"username": self._email, "password": self._password}

        try:
            response = requests.post(
                f"{self._url_base}/login/v2",
                json=data,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != requests.codes.ok:
                _LOGGER.error("Failed to login - check credentials: %s", response.text)
                return False

            payload = response.json()
            self._api_token = payload.get("token", "")
            if not self._api_token:
                _LOGGER.error("Login response missing token")
                return False

            if _LOGGER.isEnabledFor(logging.DEBUG):
                try:
                    debug_file = Path("login_debug.json")
                    debug_file.write_text(response.text, encoding="utf-8")
                    _LOGGER.debug(
                        "Full login response saved to file: %s",
                        debug_file.absolute(),
                    )
                except Exception:
                    _LOGGER.exception(
                        "Failed to write full login response to debug file"
                    )
            _LOGGER.debug("Logged in successfully")
            return self.refresh_session()

        except requests.exceptions.RequestException:
            _LOGGER.exception("Exception during login for %s", self._email)
            return False

    def refresh_session(self) -> bool:
        """Refresh session cookie/token and load accounts."""
        headers = {"x-api-key": self._api_key}
        data = {"username": self._email, "password": self._password}

        try:
            response = requests.post(
                f"{self._url_base}/login/v2/refresh",
                json=data,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != requests.codes.ok:
                _LOGGER.error("Failed to refresh session: %s", response.text)
                return False

            payload = response.json()
            self._api_session = payload.get("session", "")
            if not self._api_session:
                _LOGGER.error("Refresh response missing session")
                return False

            _LOGGER.debug("Refreshed session successfully")
            return self.get_accounts()

        except requests.exceptions.RequestException:
            _LOGGER.exception("Exception during session refresh for %s", self._email)
            return False

    def get_accounts(self) -> bool:
        """Load first account and prefer electricity contract (contractType 1)."""
        headers = {
            "x-api-key": self._api_key,
            "session": self._api_session,
        }

        try:
            response = requests.get(
                f"{self._url_base}/customer/v2?fetchAccounts=true",
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != requests.codes.ok:
                _LOGGER.error("Failed to fetch customer accounts: %s", response.text)
                return False

            data = response.json()
            accounts = data.get("accounts") or []
            if not accounts:
                _LOGGER.error("No customer accounts found in API response")
                return False

            first_account = accounts[0]
            contracts = first_account.get("contracts") or []
            if not contracts:
                _LOGGER.error("No contracts found for customer account")
                return False

            # Prefer electricity; fall back to first contract
            electricity = next(
                (c for c in contracts if str(c.get("contractType", "")) == "1"),
                contracts[0],
            )

            self._account_id = str(first_account.get("id", ""))
            self._contract_id = str(electricity.get("contractId", ""))

            if not self._account_id or not self._contract_id:
                _LOGGER.error("Missing account_id or contract_id in API response")
                return False

            _LOGGER.debug(
                "Using account_id=%s contract_id=%s",
                self._account_id,
                self._contract_id,
            )
            _LOGGER.debug("Retrieved accounts")
            return True

        except requests.exceptions.RequestException:
            _LOGGER.exception("Exception fetching accounts for %s", self._email)
            return False

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    def get_usage(
        self, year: int | str, month: int | str, day: int | str
    ) -> Any | None:
        """Fetch hourly usage for a single calendar day.

        Returns:
            list of hour dicts on success (may be empty),
            None on error / auth failure.
        """
        year_s = str(year)
        month_s = str(month).zfill(2)
        day_s = str(day).zfill(2)
        date_label = f"{year_s}-{month_s}-{day_s}"

        if not self._contract_id or not self._account_id:
            _LOGGER.warning("Missing account or contract ID — attempting login")
            if not self.login():
                return None

        headers = {
            "x-api-key": self._api_key,
            "authorization": self._api_token,
        }

        url = (
            f"{self._url_base}/usage/v2/{self._contract_id}"
            f"?ba={self._account_id}&interval=hourly"
            f"&from={date_label}&to={date_label}"
        )

        _LOGGER.debug(
            "Fetching usage → account=%s contract=%s date=%s url=%s",
            self._account_id,
            self._contract_id,
            date_label,
            url,
        )

        try:
            response = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT)

            _LOGGER.debug(
                "Usage response %s → status=%s body=%s",
                date_label,
                response.status_code,
                response.text[:200],
            )
            if _LOGGER.isEnabledFor(logging.DEBUG):
                try:
                    debug_file = Path(f"usage_debug_{date_label}.json")
                    debug_file.write_text(response.text, encoding="utf-8")
                    _LOGGER.debug(
                        "Full usage response saved to file: %s", debug_file.absolute()
                    )
                except Exception:
                    _LOGGER.exception(
                        "Failed to write full usage response to debug file"
                    )

            if response.status_code == requests.codes.ok:
                data = response.json()
                if not data:
                    _LOGGER.info(
                        "Fetched usage for %s but response was empty",
                        date_label,
                    )
                return data

            if response.status_code in (401, 403):
                _LOGGER.warning(
                    "Unauthorized for %s (status %s) — resetting token",
                    date_label,
                    response.status_code,
                )
                self._api_token = ""
                self._api_session = ""
                return None

            _LOGGER.error(
                "Failed to fetch usage for %s (status %s): %s",
                date_label,
                response.status_code,
                response.text,
            )
            return None

        except requests.exceptions.RequestException:
            _LOGGER.exception("Exception fetching usage for %s", date_label)
            return None
