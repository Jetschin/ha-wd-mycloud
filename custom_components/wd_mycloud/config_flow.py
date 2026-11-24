"""Config flow for WD My Cloud."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    WDApiGoneError,
    WDAuthError,
    WDClient,
    WDConnectionError,
    WDDevice,
    WDError,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_FOLDER,
    CONF_REFRESH_TOKEN,
    DEFAULT_FOLDER,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE = "device"

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.PASSWORD, autocomplete="current-password"
            )
        ),
    }
)


def normalize_folder(raw: str) -> str | None:
    """Normalize a folder path; None if it is empty or contains '.'/'..'."""
    parts = [p.strip() for p in raw.strip().split("/")]
    parts = [p for p in parts if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    return "/".join(parts)


class WDMyCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WD My Cloud."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow (never holds the password)."""
        self._client: WDClient | None = None
        self._devices: list[WDDevice] = []
        self._device: WDDevice | None = None
        self._refresh_token: str | None = None

    async def _login(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> tuple[WDClient, str, list[WDDevice]] | None:
        """Log in and list devices; fill `errors` on failure."""
        client = WDClient(async_get_clientsession(self.hass))
        try:
            refresh_token = await client.login(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            devices = await client.list_devices()
        except WDAuthError:
            errors["base"] = "invalid_auth"
        except WDApiGoneError:
            errors["base"] = "api_gone"
        except WDConnectionError:
            errors["base"] = "cannot_connect"
        except WDError:
            _LOGGER.exception("WD login failed")
            errors["base"] = "cannot_connect"
        else:
            if not devices:
                errors["base"] = "no_devices"
                return None
            return client, refresh_token, devices
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the connection type."""
        return self.async_show_menu(
            step_id="user", menu_options=["wd_account", "smb"]
        )

    async def async_step_smb(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Placeholder for the planned SMB mode (phase 2)."""
        return self.async_abort(reason="smb_not_yet_supported")

    async def async_step_wd_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in with the WD account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                result = await self._login(user_input, errors)
            except Exception:
                _LOGGER.exception("Unexpected error")
                return self.async_abort(reason="unknown")
            if result is not None:
                self._client, self._refresh_token, self._devices = result
                if len(self._devices) == 1:
                    self._device = self._devices[0]
                    return await self.async_step_folder()
                return await self.async_step_device()
        return self.async_show_form(
            step_id="wd_account", data_schema=CREDENTIALS_SCHEMA, errors=errors
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the device when the account has several."""
        if user_input is not None:
            self._device = next(
                d for d in self._devices if d.device_id == user_input[CONF_DEVICE]
            )
            return await self.async_step_folder()
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=d.device_id, label=d.name)
                                for d in self._devices
                            ]
                        )
                    )
                }
            ),
        )

    async def async_step_folder(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose and validate the backup folder."""
        assert self._client is not None and self._device is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            folder = normalize_folder(user_input[CONF_FOLDER])
            if folder is None:
                errors["base"] = "invalid_folder"
            elif (result := await self._create_with_folder(folder, errors)) is not None:
                return result
        return self.async_show_form(
            step_id="folder",
            data_schema=vol.Schema(
                {vol.Required(CONF_FOLDER, default=DEFAULT_FOLDER): str}
            ),
            errors=errors,
        )

    async def _create_with_folder(
        self, folder: str, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Create the folder and the entry; None (with `errors` set) on failure."""
        assert self._client is not None and self._device is not None
        await self.async_set_unique_id(f"{self._device.device_id}_{folder.lower()}")
        self._abort_if_unique_id_configured()
        self._client.select_device(self._device)
        try:
            await self._client.ensure_folder(folder)
        except WDError:
            errors["base"] = "cannot_create_folder"
            return None
        return self.async_create_entry(
            title=f"{self._device.name} ({folder})",
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_DEVICE_ID: self._device.device_id,
                CONF_FOLDER: folder,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the credentials again."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                result = await self._login(user_input, errors)
            except Exception:
                _LOGGER.exception("Unexpected error")
                return self.async_abort(reason="unknown")
            if result is not None:
                _client, refresh_token, devices = result
                # The account is the same if it still owns the configured device.
                if not any(d.device_id == entry.data[CONF_DEVICE_ID] for d in devices):
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_REFRESH_TOKEN: refresh_token},
                )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=CREDENTIALS_SCHEMA, errors=errors
        )
