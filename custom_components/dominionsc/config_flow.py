"""Config flow for Dominion SC Energy integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_ACCOUNT_NUMBER,
    CONF_BACKFILL_CYCLES_TARGET,
    CONF_DAILY_LOOKBACK_DAYS,
    CONF_PASSWORD,
    CONF_POLL_MINUTES,
    CONF_TFA_TOKEN,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_BACKFILL_CYCLES_TARGET,
    DEFAULT_DAILY_LOOKBACK_DAYS,
    DEFAULT_POLL_MINUTES,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .dominion_sc_client import DominionSCClient


_LOGGER = logging.getLogger(__name__)


class DominionSCConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Dominion SC Energy."""

    VERSION = 1

    _user_data: dict[str, Any]
    _client: DominionSCClient
    _tfa_options: list[dict[str, Any]]
    _selected_tfa_option: dict[str, Any] | None
    _is_reconfigure: bool
    _reconfig_entry: config_entries.ConfigEntry | None
    _account_options: list[dict[str, Any]]

    def __init__(self) -> None:
        self._user_data = {}
        self._client = DominionSCClient(verify_ssl=DEFAULT_VERIFY_SSL)
        self._tfa_options = []
        self._selected_tfa_option = None
        self._is_reconfigure = False
        self._reconfig_entry = None
        self._account_options = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            # Unique-id check is deferred until after we know the account_number
            # (many Dominion logins own multiple premises / accounts).

            self._user_data = {
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_VERIFY_SSL: user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                CONF_POLL_MINUTES: int(
                    user_input.get(CONF_POLL_MINUTES, DEFAULT_POLL_MINUTES)
                ),
                CONF_BACKFILL_CYCLES_TARGET: int(
                    user_input.get(
                        CONF_BACKFILL_CYCLES_TARGET, DEFAULT_BACKFILL_CYCLES_TARGET
                    )
                ),
                CONF_DAILY_LOOKBACK_DAYS: int(
                    user_input.get(
                        CONF_DAILY_LOOKBACK_DAYS, DEFAULT_DAILY_LOOKBACK_DAYS
                    )
                ),
            }

            self._client = DominionSCClient(verify_ssl=self._user_data[CONF_VERIFY_SSL])

            # If reconfiguring with unchanged credentials and a saved tfa_token,
            # pass the existing token so the client can attempt to bypass 2FA.
            if self._is_reconfigure and self._reconfig_entry:
                old_data = self._reconfig_entry.data
                if (
                    self._user_data[CONF_USERNAME] == old_data.get(CONF_USERNAME)
                    and self._user_data[CONF_PASSWORD] == old_data.get(CONF_PASSWORD)
                ):
                    existing_token = old_data.get(CONF_TFA_TOKEN)
                    if existing_token:
                        self._client.tfa_token = str(existing_token)
                        _LOGGER.debug(
                            "Reconfigure: credentials unchanged, "
                            "passing existing tfa_token to client"
                        )

            try:
                await self.hass.async_add_executor_job(
                    self._client.login,
                    self._user_data[CONF_USERNAME],
                    self._user_data[CONF_PASSWORD],
                )
                if self._client.tfa_token:
                    self._user_data[CONF_TFA_TOKEN] = self._client.tfa_token
                return await self._async_route_to_account_step()
            except Exception as err:  # pylint: disable=broad-except
                payload = err.args[0] if err.args else None
                if isinstance(payload, dict) and "2fa_required" in payload:
                    self._tfa_options = payload["2fa_required"]
                    return await self.async_step_2fa_method()
                _LOGGER.exception("Dominion SC login step failed")
                errors["base"] = "auth_failed"

        # Pre-fill with existing data during reconfigure
        default_username = ""
        default_password = ""
        default_verify_ssl = DEFAULT_VERIFY_SSL
        default_poll_minutes = DEFAULT_POLL_MINUTES
        default_backfill = DEFAULT_BACKFILL_CYCLES_TARGET
        default_lookback_days = DEFAULT_DAILY_LOOKBACK_DAYS
        if self._is_reconfigure and self._reconfig_entry:
            default_username = self._reconfig_entry.data.get(CONF_USERNAME, "")
            default_password = self._reconfig_entry.data.get(CONF_PASSWORD, "")
            default_verify_ssl = self._reconfig_entry.data.get(
                CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL
            )
            # Backwards-compat: numeric settings may be stored in data or options
            default_poll_minutes = self._reconfig_entry.options.get(
                CONF_POLL_MINUTES, self._reconfig_entry.data.get(CONF_POLL_MINUTES, DEFAULT_POLL_MINUTES)
            )
            default_backfill = self._reconfig_entry.options.get(
                CONF_BACKFILL_CYCLES_TARGET, self._reconfig_entry.data.get(CONF_BACKFILL_CYCLES_TARGET, DEFAULT_BACKFILL_CYCLES_TARGET)
            )
            default_lookback_days = self._reconfig_entry.options.get(
                CONF_DAILY_LOOKBACK_DAYS, self._reconfig_entry.data.get(CONF_DAILY_LOOKBACK_DAYS, DEFAULT_DAILY_LOOKBACK_DAYS)
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=default_username): str,
                vol.Required(CONF_PASSWORD, default=default_password): str,
                vol.Optional(CONF_VERIFY_SSL, default=default_verify_ssl): bool,
                vol.Optional(
                    CONF_POLL_MINUTES,
                    default=default_poll_minutes,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=5,
                        max=1440,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="minutes",
                    )
                ),
                vol.Optional(
                    CONF_BACKFILL_CYCLES_TARGET,
                    default=default_backfill,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=24,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="cycles",
                    )
                ),
                vol.Optional(
                    CONF_DAILY_LOOKBACK_DAYS,
                    default=default_lookback_days,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=30,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_2fa_method(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            option_index = int(user_input["option_index"])
            selected = self._tfa_options[option_index]
            try:
                await self.hass.async_add_executor_job(self._client.select_2fa_method, selected)
                self._selected_tfa_option = selected
                return await self.async_step_2fa_code()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Dominion SC 2FA send code failed")
                errors["base"] = "twofa_send_failed"

        choices = {str(i): f"{o['method']} - {o['display_value']}" for i, o in enumerate(self._tfa_options)}
        schema = vol.Schema({vol.Required("option_index", default="0"): vol.In(choices)})
        return self.async_show_form(step_id="2fa_method", data_schema=schema, errors=errors)

    async def async_step_2fa_code(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            code = str(user_input["code"]).strip()
            if not code:
                errors["base"] = "twofa_verify_failed"
                schema = vol.Schema(
                    {
                        vol.Required("code"): str,
                        vol.Optional("remember_device", default=True): bool,
                    }
                )
                return self.async_show_form(step_id="2fa_code", data_schema=schema, errors=errors)

            try:
                verified = await self.hass.async_add_executor_job(
                    self._client.verify_2fa_code,
                    code,
                    user_input.get("remember_device", True),
                )
                if not verified:
                    errors["base"] = "twofa_verify_failed"
                else:
                    if self._client.tfa_token:
                        self._user_data[CONF_TFA_TOKEN] = self._client.tfa_token
                    return await self._async_route_to_account_step()
            except Exception as err:  # pylint: disable=broad-except
                err_str = str(err).lower()
                if "invalid verification code" in err_str:
                    errors["base"] = "twofa_verify_failed"
                else:
                    errors["base"] = "twofa_post_auth_failed"
                _LOGGER.exception("Dominion SC 2FA verification step failed")

        schema = vol.Schema(
            {
                vol.Required("code"): str,
                vol.Optional("remember_device", default=True): bool,
            }
        )
        return self.async_show_form(step_id="2fa_code", data_schema=schema, errors=errors)

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return DominionSCOptionsFlow(config_entry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ):
        """Handle reconfiguration — re-runs the full auth flow with pre-filled values."""
        self._is_reconfigure = True
        self._reconfig_entry = self._get_reconfigure_entry()
        _LOGGER.debug(
            "Reconfigure flow started for entry: %s",
            self._reconfig_entry.entry_id if self._reconfig_entry else "unknown",
        )
        # Delegate to the standard user step (form will be pre-filled)
        return await self.async_step_user(user_input)

    async def _async_route_to_account_step(self):
        """After successful auth, fetch accounts and route to picker (or auto-select)."""
        try:
            accounts = await self.hass.async_add_executor_job(
                self._client.get_account_listing
            )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Dominion SC: failed to fetch account listing")
            return self.async_abort(reason="account_listing_failed")

        if not accounts:
            return self.async_abort(reason="no_accounts_found")

        self._account_options = accounts

        # Single account — auto-select, no extra prompt.
        if len(accounts) == 1:
            self._user_data[CONF_ACCOUNT_NUMBER] = str(accounts[0].get("account_number", ""))
            return await self._async_finish()

        # Multi-account — show the picker.
        return await self.async_step_account()

    async def async_step_account(self, user_input: dict[str, Any] | None = None):
        """Pick which Dominion account/premise this HA instance should track."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._user_data[CONF_ACCOUNT_NUMBER] = str(user_input[CONF_ACCOUNT_NUMBER])
            return await self._async_finish()

        # Map the opaque (encrypted) account_number to a friendly label.
        # Dominion returns accountNumber as a base64 blob used server-side,
        # and accountNumberFormatted like "1-5583" for display.
        choices: dict[str, str] = {}
        for a in self._account_options:
            acct = str(a.get("account_number", ""))
            friendly = a.get("account_number_formatted") or acct
            addr = a.get("service_address") or a.get("address") or a.get("premise") or ""
            label = friendly + (f" — {addr}" if addr else "")
            choices[acct] = label

        schema = vol.Schema({vol.Required(CONF_ACCOUNT_NUMBER): vol.In(choices)})
        return self.async_show_form(step_id="account", data_schema=schema, errors=errors)

    async def _async_finish(self):
        """Create or update the config entry after successful auth + account selection."""
        username = self._user_data.get(CONF_USERNAME, "")
        account_number = self._user_data.get(CONF_ACCOUNT_NUMBER, "")
        unique_id = f"{username}_{account_number}" if account_number else username

        # Look up a friendly label / address from account_options for the title.
        friendly = ""
        address = ""
        for a in self._account_options:
            if str(a.get("account_number", "")) == account_number:
                friendly = a.get("account_number_formatted") or ""
                address = a.get("service_address") or ""
                break

        if self._is_reconfigure and self._reconfig_entry:
            _LOGGER.debug(
                "Reconfigure: updating entry %s (unique_id=%s)",
                self._reconfig_entry.entry_id,
                unique_id,
            )
            return self.async_update_reload_and_abort(
                self._reconfig_entry,
                data=self._user_data,
                unique_id=unique_id,
            )

        # New entry — enforce uniqueness now that we know account_number
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        # Prefer friendly "1-5583 — 2421 BERMUDA HILLS RD" title over encrypted blob
        if friendly and address:
            title = f"Dominion SC {friendly} — {address}"
        elif friendly:
            title = f"Dominion SC {friendly}"
        elif account_number:
            title = f"Dominion SC ({account_number[:12]}…)"
        else:
            title = "Dominion SC Energy"
        return self.async_create_entry(title=title, data=self._user_data)


class DominionSCOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._entry.options

        # Fall back to data for entries created before options were separated
        def _get_current(key: str, fallback):
            return current.get(key, self._entry.data.get(key, fallback))

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_POLL_MINUTES,
                    description={
                        "suggested_value": _get_current(
                            CONF_POLL_MINUTES, DEFAULT_POLL_MINUTES
                        )
                    },
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=5,
                        max=1440,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="minutes",
                    )
                ),
                vol.Optional(
                    CONF_BACKFILL_CYCLES_TARGET,
                    description={
                        "suggested_value": _get_current(
                            CONF_BACKFILL_CYCLES_TARGET,
                            DEFAULT_BACKFILL_CYCLES_TARGET,
                        )
                    },
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=24,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="cycles",
                    )
                ),
                vol.Optional(
                    CONF_DAILY_LOOKBACK_DAYS,
                    description={
                        "suggested_value": _get_current(
                            CONF_DAILY_LOOKBACK_DAYS,
                            DEFAULT_DAILY_LOOKBACK_DAYS,
                        )
                    },
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=30,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
