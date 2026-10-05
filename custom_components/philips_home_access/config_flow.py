"""Config flow for Philips Home Access (email + password)."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_AREACODE, DEFAULT_AREACODE, DOMAIN
from .homeaccess import AuthError, HomeAccess, HomeAccessConnectionError, Settings

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema({
    vol.Required(CONF_EMAIL): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Optional(CONF_AREACODE, default=DEFAULT_AREACODE): str,
})


class PhilipsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config + reauth flow."""

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
            uid = await self._try_verify(user_input, errors, reason)
            if uid is not None:
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL], data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors,
            description_placeholders=reason)

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
