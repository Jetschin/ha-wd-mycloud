"""WD My Cloud integration for Home Assistant."""

from __future__ import annotations

from collections.abc import Callable
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.hass_dict import HassKey

from .api import (
    WDApiGoneError,
    WDAuthError,
    WDClient,
    WDConnectionError,
    WDError,
)
from .const import CONF_DEVICE_ID, CONF_FOLDER, CONF_REFRESH_TOKEN, DOMAIN
from .issues import async_clear_api_gone, async_raise_api_gone

_LOGGER = logging.getLogger(__name__)

type WDConfigEntry = ConfigEntry[WDClient]

DATA_BACKUP_AGENT_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(
    f"{DOMAIN}.backup_agent_listeners"
)


async def async_setup_entry(hass: HomeAssistant, entry: WDConfigEntry) -> bool:
    """Set up WD My Cloud from a config entry."""

    def _store_token(token: str) -> None:
        # WD may rotate the refresh token; persist it or reauth is needed at restart.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    client = WDClient(
        async_get_clientsession(hass),
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        on_token_update=_store_token,
    )

    try:
        await client.ensure_token()
        devices = await client.list_devices()
        device = next(
            (d for d in devices if d.device_id == entry.data[CONF_DEVICE_ID]), None
        )
        if device is None:
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key="device_not_found"
            )
        client.select_device(device)
        client.folder_id = await client.ensure_folder(entry.data[CONF_FOLDER])
    except WDAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except WDApiGoneError as err:
        async_raise_api_gone(hass)
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="api_gone"
        ) from err
    except WDConnectionError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err
    except WDError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from err

    async_clear_api_gone(hass)
    entry.runtime_data = client

    def async_notify_backup_listeners() -> None:
        for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
            listener()

    entry.async_on_unload(entry.async_on_state_change(async_notify_backup_listeners))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WDConfigEntry) -> bool:
    """Unload a config entry."""
    return True
