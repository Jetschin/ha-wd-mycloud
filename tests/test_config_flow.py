"""Tests for the WD My Cloud config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
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
DEV2 = WDDevice("dev2", "Other Cloud", "https://i2", "https://e2")


@pytest.fixture(autouse=True)
def no_setup():
    with patch(
        "custom_components.wd_mycloud.async_setup_entry", return_value=True
    ) as m:
        yield m


@pytest.fixture
def client():
    with patch("custom_components.wd_mycloud.config_flow.WDClient") as cls:
        c = MagicMock()
        c.login = AsyncMock(return_value="R1")
        c.list_devices = AsyncMock(return_value=[DEV1])
        c.ensure_folder = AsyncMock(return_value="fid")
        c.user_id = "auth0|u1"
        cls.return_value = c
        yield c


async def _to_account(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "wd_account"}
    )


async def _login(hass, flow_id):
    return await hass.config_entries.flow.async_configure(
        flow_id, {"email": "a@b.c", "password": "secret"}
    )


async def test_full_flow_single_device(hass, client):
    result = await _to_account(hass)
    assert result["step_id"] == "wd_account"
    result = await _login(hass, result["flow_id"])
    assert result["step_id"] == "folder"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"folder": " /HA_Backup// "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My Cloud Home (HA_Backup)"
    assert set(result["data"]) == {"refresh_token", "device_id", "folder"}
    assert "password" not in result["data"]
    assert result["data"] == {
        CONF_REFRESH_TOKEN: "R1",
        CONF_DEVICE_ID: "dev1",
        CONF_FOLDER: "HA_Backup",
    }
    assert result["result"].unique_id == "dev1_ha_backup"
    assert not result["result"].options
    client.select_device.assert_called_once_with(DEV1)


async def test_flow_multiple_devices(hass, client):
    client.list_devices.return_value = [DEV1, DEV2]
    result = await _to_account(hass)
    result = await _login(hass, result["flow_id"])
    assert result["step_id"] == "device"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": "dev2"}
    )
    assert result["step_id"] == "folder"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"folder": "HA_Backup"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_ID] == "dev2"
    assert result["title"] == "Other Cloud (HA_Backup)"


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (WDAuthError("x"), "invalid_auth"),
        (WDConnectionError("x"), "cannot_connect"),
        (WDApiGoneError("x"), "api_gone"),
    ],
)
async def test_flow_login_errors(hass, client, exc, error):
    client.login.side_effect = exc
    result = await _to_account(hass)
    result = await _login(hass, result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    client.login.side_effect = None
    result = await _login(hass, result["flow_id"])
    assert result["step_id"] == "folder"


async def test_flow_invalid_auth(hass, client):
    client.login.side_effect = WDAuthError("bad")
    result = await _login(hass, (await _to_account(hass))["flow_id"])
    assert result["errors"] == {"base": "invalid_auth"}


async def test_flow_cannot_connect(hass, client):
    client.list_devices.side_effect = WDConnectionError("down")
    result = await _login(hass, (await _to_account(hass))["flow_id"])
    assert result["errors"] == {"base": "cannot_connect"}


async def test_flow_no_devices(hass, client):
    client.list_devices.return_value = []
    result = await _login(hass, (await _to_account(hass))["flow_id"])
    assert result["errors"] == {"base": "no_devices"}


async def test_flow_unexpected_error_aborts(hass, client):
    client.login.side_effect = RuntimeError("boom")
    result = await _login(hass, (await _to_account(hass))["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"


async def test_flow_cannot_create_folder(hass, client):
    client.ensure_folder.side_effect = WDConnectionError("x")
    result = await _login(hass, (await _to_account(hass))["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"folder": "HA_Backup"}
    )
    assert result["errors"] == {"base": "cannot_create_folder"}


@pytest.mark.parametrize(
    "raw", ["", "  ", "/", "//", "a/../b", "..", ".", "a/./b", " / . / "]
)
async def test_flow_invalid_folder(hass, client, raw):
    result = await _login(hass, (await _to_account(hass))["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"folder": raw}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_folder"}
    client.ensure_folder.assert_not_called()


async def test_flow_folder_normalized_nested(hass, client):
    result = await _login(hass, (await _to_account(hass))["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"folder": " //Backups//Home Assistant/Würfel/ "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_FOLDER] == "Backups/Home Assistant/Würfel"
    assert result["title"] == "My Cloud Home (Backups/Home Assistant/Würfel)"
    assert result["result"].unique_id == "dev1_backups/home assistant/würfel"
    client.ensure_folder.assert_awaited_with("Backups/Home Assistant/Würfel")


async def test_flow_smb_aborts(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "smb"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "smb_not_yet_supported"


async def test_flow_already_configured(hass, client):
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev1_ha_backup",
        data={CONF_REFRESH_TOKEN: "x", CONF_DEVICE_ID: "dev1", CONF_FOLDER: "HA_Backup"},
    ).add_to_hass(hass)
    result = await _login(hass, (await _to_account(hass))["flow_id"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"folder": "/ha_backup"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


def _entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="dev1_ha_backup",
        data={CONF_REFRESH_TOKEN: "old", CONF_DEVICE_ID: "dev1", CONF_FOLDER: "HA_Backup"},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_success(hass, client):
    entry = _entry(hass)
    client.login.return_value = "NEW"
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await _login(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "NEW"
    assert entry.data[CONF_DEVICE_ID] == "dev1"
    assert "password" not in entry.data


async def test_reauth_wrong_account(hass, client):
    entry = _entry(hass)
    client.list_devices.return_value = [DEV2]
    result = await entry.start_reauth_flow(hass)
    result = await _login(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_REFRESH_TOKEN] == "old"


async def test_reauth_invalid_auth(hass, client):
    entry = _entry(hass)
    client.login.side_effect = WDAuthError("bad")
    result = await entry.start_reauth_flow(hass)
    result = await _login(hass, result["flow_id"])
    assert result["errors"] == {"base": "invalid_auth"}
