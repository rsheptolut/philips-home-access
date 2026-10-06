"""Config flow for Philips Home Access (email + password)."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_AREACODE, CONF_STATE_ONLY, DEFAULT_AREACODE, DOMAIN
from .homeaccess import AuthError, HomeAccess, HomeAccessConnectionError, Settings

_LOGGER = logging.getLogger(__name__)

# No default area code: the one chosen at signup is the user's to give (a wrong
# default once quietly applied 61/Australia to everyone).
USER_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Required(CONF_AREACODE): str,
})


def _clean_areacode(value: str) -> str:
    """"+61 " -> "61"; the cloud wants bare digits."""
    return "".join(value.split()).lstrip("+")


class PhilipsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config + reauth flow."""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PhilipsOptionsFlow()

    async def _verify(self, data: Mapping[str, Any]) -> str:
        """Return the account uid, or raise AuthError / HomeAccessConnectionError."""
        settings = Settings(
            identifier=data[CONF_EMAIL], credential=data[CONF_PASSWORD],
            areacode=data.get(CONF_AREACODE, DEFAULT_AREACODE))
        async with HomeAccess(settings, session=async_get_clientsession(self.hass)) as ha:
            return await ha.async_verify_credentials()

    async def _try_verify(
        self, data: Mapping[str, Any], errors: dict[str, str], reason: dict[str, str]
    ) -> str | None:
        """_verify, mapping failures to a form error plus the {reason} shown with it.

        The cloud's own refusal (e.g. "Account does not exist (code 1004)") is
        what tells a user what to fix; a bare "invalid email or password" hides it.
        """
        try:
            return await self._verify(data)
        except AuthError as e:
            errors["base"], reason["reason"] = "invalid_auth", e.reason
        except HomeAccessConnectionError as e:
            _LOGGER.warning("Sign-in failed: %s", e)
            errors["base"], reason["reason"] = "cannot_connect", str(e)
        except Exception:  # noqa: BLE001 - surface anything else, don't swallow it
            _LOGGER.exception("Unexpected error during sign-in")
            errors["base"] = "unknown"
            reason["reason"] = "see the Home Assistant log for details"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reason = {"reason": ""}
        if user_input is not None:
            user_input = {**user_input,
                          CONF_EMAIL: user_input[CONF_EMAIL].strip(),
                          CONF_AREACODE: _clean_areacode(user_input[CONF_AREACODE])}
            if not user_input[CONF_AREACODE].isdigit():
                errors[CONF_AREACODE] = "invalid_areacode"
            elif (uid := await self._try_verify(user_input, errors, reason)) is not None:
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL], data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, {
                k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}),
            errors=errors, description_placeholders=reason)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        reason = {"reason": ""}
        if user_input is not None:
            data = {**entry.data, **user_input}
            if await self._try_verify(data, errors, reason) is not None:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={CONF_EMAIL: entry.data.get(CONF_EMAIL, ""),
                                      **reason},
            errors=errors,
        )


class PhilipsOptionsFlow(OptionsFlow):
    """Pick the locks to show as state only (no lock/unlock)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")
        locks = {esn: (coordinator.locks[esn].nickname or esn)
                 for esn in coordinator.lock_esns() if esn in coordinator.locks}
        if not locks:
            return self.async_abort(reason="no_locks")
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_STATE_ONLY: list(user_input.get(CONF_STATE_ONLY, []))})
        current = [esn for esn in self.config_entry.options.get(CONF_STATE_ONLY, [])
                   if esn in locks]
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Optional(CONF_STATE_ONLY, default=current): cv.multi_select(locks),
        }))
