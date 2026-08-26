"""Contact Energy API."""

import logging
import requests

_LOGGER = logging.getLogger(__name__)


class ContactEnergyApi:
    """Class for Contact Energy API."""

    def __init__(self, email, password):
        """Initialise Contact Energy API."""
        self._api_token = ""
        self._api_session = ""
        self._contractId = ""
        self._accountId = ""
        self._url_base = "https://api.contact-digital-prod.net"
        self._api_key = "z840P4lQCH9TqcjC9L2pP157DZcZJMcr5tVQCvyx"
        self._email = email
        self._password = password

    def login(self):
        """Login to the Contact Energy API."""
        headers = {"x-api-key": self._api_key}
        data = {"username": self._email, "password": self._password}
        
        try:
            loginResult = requests.post(
                self._url_base + "/login/v2", json=data, headers=headers, timeout=15
            )
            if loginResult.status_code == requests.codes.ok:
                jsonResult = loginResult.json()
                self._api_token = jsonResult.get("token", "")
                _LOGGER.debug("Logged in successfully")
                return self.refresh_session()
            
            _LOGGER.error(
                "Failed to login - check credentials: %s", loginResult.text
            )
            return False
        except Exception as err:
            _LOGGER.error("Exception during login: %s", err)
            return False

    def refresh_session(self):
        """Refresh the session and update contract details."""
        headers = {"x-api-key": self._api_key}
        data = {"token": self._api_token}
        
        try:
            loginResult = requests.post(
                self._url_base + "/login/v2/refresh", json=data, headers=headers, timeout=15
            )
            if loginResult.status_code == requests.codes.ok:
                jsonResult = loginResult.json()
                self._api_session = jsonResult.get("session", "")
                _LOGGER.debug("Refreshed session successfully")
                return self.get_accounts()
            
            _LOGGER.error(
                "Failed to refresh session: %s", loginResult.text
            )
            return False
        except Exception as err:
            _LOGGER.error("Exception during session refresh: %s", err)
            return False

    def get_accounts(self):
        """Get the first account and contract ID."""
        headers = {"x-api-key": self._api_key, "session": self._api_session}
        try:
            result = requests.get(
                self._url_base + "/customer/v2?fetchAccounts=true", headers=headers, timeout=15
            )
            if result.status_code == requests.codes.ok:
                _LOGGER.debug("Retrieved accounts")
                data = result.json()
                accounts = data.get("accounts", [])
                if accounts:
                    self._accountId = accounts[0]["id"]
                    self._contractId = accounts[0]["contracts"][0]["contractId"]
                    return True
                _LOGGER.error("No customer accounts found in API response")
                return False
            
            _LOGGER.error("Failed to fetch customer accounts: %s", result.text)
            return False
        except Exception as err:
            _LOGGER.error("Exception fetching accounts: %s", err)
            return False

    def get_usage(self, year, month, day):
        """Fetch usage data for a given date."""
        if not self._contractId or not self._accountId:
            _LOGGER.warning("Missing account or contract ID. Attempting login...")
            if not self.login():
                return None

        formatted_month = str(month).zfill(2)
        formatted_day = str(day).zfill(2)
        date_str = f"{year}-{formatted_month}-{formatted_day}"

        # Standard Bearer Authorization header
        headers = {
            "x-api-key": self._api_key, 
            "authorization": f"Bearer {self._api_token}" if not self._api_token.startswith("Bearer ") else self._api_token
        }
        
        url = (
            f"{self._url_base}/usage/v2/{self._contractId}"
            f"?ba={self._accountId}&interval=hourly&from={date_str}&to={date_str}"
        )

        try:
            response = requests.post(url, headers=headers, timeout=15)
            
            if response.status_code == requests.codes.ok:
                data = response.json()
                if not data:
                    _LOGGER.info("Fetched usage data for %s, but response was empty", date_str)
                return data

            if response.status_code in (401, 403):
                _LOGGER.warning("Token expired or unauthorized for %s (Status %s). Resetting token.", date_str, response.status_code)
                self._api_token = ""
                return None

            _LOGGER.error(
                "Failed to fetch usage data for %s (Status %s): %s", 
                date_str, response.status_code, response.text
            )
            return None

        except Exception as err:
            _LOGGER.error("Exception fetching usage for %s: %s", date_str, err)
            return None