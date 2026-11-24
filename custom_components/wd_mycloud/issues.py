"""Repair issues for WD My Cloud.

Named ``issues`` (not ``repairs``) because Home Assistant treats a module called
``repairs`` as a repairs platform and requires ``async_create_fix_flow``.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUE_API_GONE = "api_gone"


def async_raise_api_gone(hass: HomeAssistant) -> None:
    """Tell the user that WD has switched off the cloud API."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_API_GONE,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_API_GONE,
    )


def async_clear_api_gone(hass: HomeAssistant) -> None:
    """Remove the API-gone issue (no-op if absent)."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_API_GONE)
