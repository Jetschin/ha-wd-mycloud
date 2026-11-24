"""Shared test fixtures."""

from __future__ import annotations

import base64
import json

import pytest
from aioresponses import aioresponses


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of custom integrations in all tests."""


@pytest.fixture
def mock_wd():
    """Mock all aiohttp traffic."""
    with aioresponses() as m:
        yield m


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def make_jwt(sub: str, exp: int) -> str:
    """Build an unsigned fake JWT."""
    return f"{_b64({'alg': 'none'})}.{_b64({'sub': sub, 'exp': exp})}.sig"
