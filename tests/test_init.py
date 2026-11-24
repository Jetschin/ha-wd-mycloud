"""Tests for WD My Cloud setup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wd_mycloud.api import (
    WDApiGoneError,
    WDAuthError,
    WDConnectionError,
    WDDevice,
)
from custom_components.wd_mycloud.const import (
    CONF_DEVICE_ID,
    CONF_FOLDER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

DEV1 = WDDevice("dev1", "My Cloud Home", "https://i1", "https://e1")


@pytest.fixture
def entry(hass):
    e = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev1_ha_backup",
        data={CONF_REFRESH_TOKEN: "R1", CONF_DEVICE_ID: "dev1", CONF_FOLDER: "HA_Backup"},
    )
    e.add_to_hass(hass)
    return e


@pytest.fixture
def client_cls():
    with patch("custom_components.wd_mycloud.WDClient") as cls:
        c = MagicMock()
        c.ensure_token = AsyncMock()
        c.list_devices = AsyncMock(return_value=[DEV1])
        c.ensure_folder = AsyncMock(return_value="fid")
        cls.return_value = c
        yield cls


async def test_setup_ok(hass, entry, client_cls):
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.folder_id == "fid"
    entry.runtime_data.select_device.assert_called_once_with(DEV1)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_auth_failed_starts_reauth(hass, entry, client_cls):
    client_cls.return_value.ensure_token.side_effect = WDAuthError("revoked")
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_setup_not_ready_on_connection_error(hass, entry, client_cls):
    client_cls.return_value.ensure_token.side_effect = WDConnectionError("down")
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_not_ready_on_api_gone(hass, entry, client_cls):
    client_cls.return_value.ensure_token.side_effect = WDApiGoneError("gone")
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_device_not_found(hass, entry, client_cls):
    client_cls.return_value.list_devices.return_value = []
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_folder_connection_error(hass, entry, client_cls):
    client_cls.return_value.ensure_folder.side_effect = WDConnectionError("x")
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_token_rotation_persisted(hass, entry, client_cls):
    def make(session, refresh_token=None, on_token_update=None):
        c = client_cls.return_value

        async def ensure():
            on_token_update("R2")

        c.ensure_token.side_effect = ensure
        return c

    client_cls.side_effect = make
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_REFRESH_TOKEN] == "R2"
    assert entry.data[CONF_DEVICE_ID] == "dev1"
    assert entry.data[CONF_FOLDER] == "HA_Backup"


async def test_api_gone_raises_issue(hass, entry, client_cls):
    client_cls.return_value.ensure_token.side_effect = WDApiGoneError("gone")
    await hass.config_entries.async_setup(entry.entry_id)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "api_gone")
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "api_gone"


async def test_issue_cleared_on_success(hass, entry, client_cls):
    ir.async_create_issue(
        hass,
        DOMAIN,
        "api_gone",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="api_gone",
    )
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, "api_gone") is None
