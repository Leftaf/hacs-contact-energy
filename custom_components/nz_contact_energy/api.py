"""Contact Energy API."""

import logging
from typing import Any

import requests

_LOGGER = logging.getLogger(__name__)


class ContactEnergyApi:
    """Class for Contact Energy API."""

    def __init__(
        self,
        email: str,
        password: str,
        api_key: str = "z840P4lQCH9TqcjC9L2pP157DZcZJMcr5tVQCvyx",
    ) -> None:
        """Initialise Contact Energy API."""
        self._api_token = ""
        self._api_session = ""
        self._contract_id = ""
        self._account_id = ""
        self._url_base = "https://api.contact-digital-prod.net"
        self._api_key = api_key
        self._email = email
        self._password = password

    def login(self) -> bool:
        """Login to the Contact Energy API."""
        headers = {"x-api-key": self._api_key}
        data = {"username": self._email, "password": self._password}

        try:
            loginresult = requests.post(
                f"{self._url_base}/login/v2", json=data, headers=headers, timeout=15
            )
            if loginresult.status_code == requests.codes.ok:
                jsonresult = loginresult.json()
                self._api_token = jsonresult.get("token", "")
                _LOGGER.debug("Login successfully")
                return self.refresh_session()

            _LOGGER.error("Failed to login - check credentials: %s", loginresult.text)
        except requests.exceptions.RequestException:
            _LOGGER.exception("Exception during login for: %s", self._email)

        return False

    def refresh_session(self) -> bool:
        """Refresh the session and update contract details."""
        headers = {"x-api-key": self._api_key}
        data = {
        "username": self._email,
        "password": self._password
        }

        try:
            loginresult = requests.post(
                f"{self._url_base}/login/v2/refresh",
                json=data,
                headers=headers,
                timeout=15,
            )
            if loginresult.status_code == requests.codes.ok:
                jsonresult = loginresult.json()
                self._api_session = jsonresult.get("session", "")
                _LOGGER.debug("Refreshed session successfully")
                return self.get_accounts()

            _LOGGER.error("Failed to refresh session: %s", loginresult.text)
        except requests.exceptions.RequestException:
            _LOGGER.exception("Exception during session refresh for: %s", self._email)

        return False

    def get_accounts(self) -> bool:
        """Get the first account and contract ID."""
        headers = {
            "x-api-key": self._api_key,
            "session": self._api_session
        }
        try:
            result = requests.get(
                f"{self._url_base}/customer/v2?fetchAccounts=true",
                headers=headers
            )
            if result.status_code == requests.codes.ok:
                _LOGGER.debug("Retrieved accounts")
                data = result.json()
                accounts = data.get("accounts", [])

                if accounts:
                    first_account = accounts[0]
                    contracts = first_account.get("contracts", [])
                    if contracts:
                        # Prefer electricity contract (contractType == 1)
                        electricity = next(
                        (c for c in contracts if c.get("contractType") == "1"),
                        contracts[0]
                        )
                        self._contract_id = electricity.get("contractId", "")
                        self._account_id = first_account.get("id", "")

                        _LOGGER.debug(
                            "Using account_id=%s contract_id=%s",
                            self._account_id,
                            self._contract_id,
                        )

                        return True
                    _LOGGER.error("No contracts found for customer account")
                    return False

                _LOGGER.error("No customer accounts found in API response")
                return False

            _LOGGER.error("Failed to fetch customer accounts: %s", result.text)
        except requests.exceptions.RequestException:
            _LOGGER.exception("Exception fetching accounts for: %s", self._email)

        return False

    def get_usage(self, year, month, day) -> Any | None:
        """Fetch usage data for a given date."""
        # Accept both int and str
        year = str(year)
        month = str(month).zfill(2)
        day = str(day).zfill(2)

        if not self._contract_id or not self._account_id:
            _LOGGER.warning("Missing account or contract ID. Attempting login...")
            if not self.login():
                return None

        headers = {
            "x-api-key": self._api_key,
            "authorization": self._api_token,
        }

        url = (
            f"{self._url_base}/usage/v2/{self._contract_id}"
            f"?ba={self._account_id}&interval=hourly"
            f"&from={year}-{month}-{day}&to={year}-{month}-{day}"
        )

        _LOGGER.debug(
            "Fetching usage → Account: %s | Contract: %s | Date: %s/%s/%s | URL: %s",
            self._account_id,
            self._contract_id,
            year,
            month,
            day,
            url,
        )

        try:
            response = requests.post(url, headers=headers)

            _LOGGER.debug(
            "Usage response for %s-%s-%s → Status: %s | Body: %s",
            year,
            month,
            day,
            response.status_code,
            response.text[:300],   # only first 300 characters
            )

            if response.status_code == requests.codes.ok:
                data = response.json()
                if not data:
                    _LOGGER.info(
                    "Fetched usage data for %s/%s/%s, but response was empty",
                    year,
                    month,
                    day
                    )
                return data

            if response.status_code in (401, 403):
                _LOGGER.warning(
                    "Token expired or unauthorized for %s/%s/%s (Status %s). Resetting token.",
                    year,
                    month,
                    day,
                    response.status_code,
                )
                self._api_token = ""
                self._api_session = ""
                return None

            _LOGGER.error(
            "Failed to fetch usage data for %s/%s/%s (Status %s): %s",
            year,
            month,
            day,
            response.status_code,
            response.text,
            )
            _LOGGER.debug(response)
        except requests.exceptions.RequestException:
            _LOGGER.exception("Exception fetching usage for %s/%s/%s", year, month, day)

        return None