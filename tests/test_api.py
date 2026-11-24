"""Tests for the WD auth client."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.wd_mycloud.api import (
    WDApiGoneError,
    WDAuthError,
    WDClient,
    WDConnectionError,
)
from custom_components.wd_mycloud.const import CONFIG_URL

from .conftest import make_jwt

AUTH = "https://auth.test"
TOKEN_URL = f"{AUTH}/oauth/token"
CONFIG = {
    "data": {
        "componentMap": {
            "cloud.service.urls": {
                "service.auth0.url": AUTH,
                "service.device.url": "https://dev.test",
            }
        }
    }
}


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def _tokens(refresh="R1", exp_in=3600, id_token=True):
    body = {
        "access_token": make_jwt("auth0|u1", int(time.time()) + exp_in),
        "expires_in": exp_in,
    }
    if refresh:
        body["refresh_token"] = refresh
    if id_token:
        body["id_token"] = make_jwt("auth0|u1", int(time.time()) + exp_in)
    return body


async def test_login_success(session, mock_wd):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, payload=_tokens())
    client = WDClient(session)
    assert await client.login("a@b.c", "pw") == "R1"
    assert client.user_id == "auth0|u1"
    assert client.refresh_token == "R1"
    (call,) = mock_wd.requests[("POST", aiohttp.client.URL(TOKEN_URL))]
    sent = call.kwargs["json"]
    assert sent["grant_type"] == "http://auth0.com/oauth/grant-type/password-realm"
    assert sent["audience"] == "mycloud.com"
    assert sent["scope"] == (
        "openid offline_access nas_read_write nas_read_only user_read device_read"
    )
    assert sent["username"] == "a@b.c"
    json.dumps(sent)


async def test_login_bad_credentials(session, mock_wd):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, status=403, payload={"error": "invalid_grant"})
    with pytest.raises(WDAuthError):
        await WDClient(session).login("a@b.c", "bad")


@pytest.mark.parametrize("status", [404, 410])
async def test_token_endpoint_gone_on_login(session, mock_wd, status):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, status=status)
    with pytest.raises(WDApiGoneError):
        await WDClient(session).login("a@b.c", "pw")


@pytest.mark.parametrize("status", [404, 410])
async def test_token_endpoint_gone_on_refresh(session, mock_wd, status):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, status=status)
    with pytest.raises(WDApiGoneError):
        await WDClient(session, refresh_token="R1").ensure_token()


async def test_config_gone(session, mock_wd):
    mock_wd.get(CONFIG_URL, status=404)
    with pytest.raises(WDApiGoneError):
        await WDClient(session).login("a@b.c", "pw")


async def test_config_missing_keys(session, mock_wd):
    mock_wd.get(CONFIG_URL, payload={"data": {}})
    with pytest.raises(WDApiGoneError):
        await WDClient(session).login("a@b.c", "pw")


async def test_network_error(session, mock_wd):
    mock_wd.get(CONFIG_URL, exception=aiohttp.ClientConnectionError("boom"))
    with pytest.raises(WDConnectionError):
        await WDClient(session).login("a@b.c", "pw")


async def test_server_error_is_connection_error(session, mock_wd):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, status=502)
    with pytest.raises(WDConnectionError):
        await WDClient(session).login("a@b.c", "pw")


async def test_refresh_rotates_token(session, mock_wd):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, payload=_tokens(refresh="R2", id_token=False))
    spy = MagicMock()
    client = WDClient(session, refresh_token="R1", on_token_update=spy)
    await client.ensure_token()
    spy.assert_called_once_with("R2")
    assert client.refresh_token == "R2"
    assert client.user_id == "auth0|u1"
    # Valid token: no further HTTP request (aioresponses would raise otherwise).
    await client.ensure_token()
    spy.assert_called_once()
    assert len(mock_wd.requests[("POST", aiohttp.client.URL(TOKEN_URL))]) == 1


async def test_refresh_same_token_no_callback(session, mock_wd):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, payload=_tokens(refresh=None, id_token=False))
    spy = MagicMock()
    client = WDClient(session, refresh_token="R1", on_token_update=spy)
    await client.ensure_token()
    spy.assert_not_called()
    assert client.refresh_token == "R1"


async def test_refresh_invalid(session, mock_wd):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, status=403, payload={"error": "invalid_grant"})
    with pytest.raises(WDAuthError):
        await WDClient(session, refresh_token="R1").ensure_token()


async def test_authed_sends_bearer(session, mock_wd):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    tokens = _tokens()
    mock_wd.post(TOKEN_URL, payload=tokens)
    mock_wd.get("https://dev.test/x", payload={"ok": 1})
    client = WDClient(session, refresh_token="R1")
    resp = await client._authed("GET", "https://dev.test/x")
    assert resp.status == 200
    resp.release()
    (call,) = mock_wd.requests[("GET", aiohttp.client.URL("https://dev.test/x"))]
    assert call.kwargs["headers"]["Authorization"] == f"Bearer {tokens['access_token']}"


# --- Task 2: device discovery and file operations -----------------------------

import re
from urllib.parse import parse_qs, urlparse

from aioresponses import CallbackResult

from custom_components.wd_mycloud import api as api_mod
from custom_components.wd_mycloud.api import (
    WDDevice,
    WDFile,
    WDNotFoundError,
)

INT = "https://d1.local.test"
EXT = "https://ext.test"
DIR_MIME = "application/x.wd.dir"


def _rx(base: str, path: str) -> re.Pattern:
    return re.compile(r"^" + re.escape(base + path) + r"(\?.*)?$")


async def _ready(session, mock_wd, internal=INT, external=EXT) -> WDClient:
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, payload=_tokens())
    client = WDClient(session, refresh_token="R1")
    client.select_device(WDDevice("d1", "NAS", internal, external))
    return client


def _recorder(calls, status=200, payload=None, headers=None, body=""):
    def cb(url, **kw):
        calls.append((str(url), kw))
        return CallbackResult(
            status=status, payload=payload, headers=headers, body=body
        )

    return cb


def _meta_from_multipart(data: bytes) -> dict:
    text = data.decode()
    assert "Content-Type: application/json" in text
    return json.loads(text.split("\r\n\r\n", 1)[1].rsplit("\r\n--", 1)[0])


async def test_list_devices(session, mock_wd):
    mock_wd.get(CONFIG_URL, payload=CONFIG)
    mock_wd.post(TOKEN_URL, payload=_tokens())
    mock_wd.get(
        "https://dev.test/device/v1/user/auth0|u1",
        payload={
            "data": [
                {
                    "deviceId": "d1",
                    "name": "My Cloud Home",
                    "network": {
                        "internalDNSName": "d1.local.test",
                        "externalURI": "https://ext.test",
                    },
                }
            ]
        },
    )
    client = WDClient(session, refresh_token="R1")
    assert await client.list_devices() == [
        WDDevice("d1", "My Cloud Home", INT, EXT)
    ]


async def test_list_folder_paginates(session, mock_wd):
    client = await _ready(session, mock_wd)
    calls: list = []
    mock_wd.get(
        _rx(INT, "/sdk/v2/filesSearch/parents"),
        callback=_recorder(
            calls,
            payload={
                "files": [
                    {"id": "a", "name": "A", "mimeType": DIR_MIME},
                    {"id": "b", "name": "b.tar", "mimeType": "x/y", "size": 5},
                ],
                "pageToken": "T2",
            },
        ),
    )
    mock_wd.get(
        _rx(INT, "/sdk/v2/filesSearch/parents"),
        callback=_recorder(
            calls,
            payload={"files": [{"id": "c", "name": "c", "mimeType": "x/y", "size": 1}]},
        ),
    )
    files = await client.list_folder("root")
    assert files == [
        WDFile("a", "A", True, 0),
        WDFile("b", "b.tar", False, 5),
        WDFile("c", "c", False, 1),
    ]
    q1 = parse_qs(urlparse(calls[0][0]).query)
    q2 = parse_qs(urlparse(calls[1][0]).query)
    assert q1["ids"] == ["root"] and "pageToken" not in q1
    assert q2["pageToken"] == ["T2"]


async def test_ensure_folder_nested_unicode(session, mock_wd):
    client = await _ready(session, mock_wd)
    finds: list = []
    creates: list = []
    # "Backups" exists; the others do not.
    mock_wd.get(
        _rx(INT, "/sdk/v2/filesSearch/parentAndName"),
        callback=_recorder(
            finds, payload={"id": "F1", "name": "Backups", "mimeType": DIR_MIME}
        ),
    )
    mock_wd.get(
        _rx(INT, "/sdk/v2/filesSearch/parentAndName"), status=404
    )
    mock_wd.get(
        _rx(INT, "/sdk/v2/filesSearch/parentAndName"), status=404
    )
    mock_wd.post(
        _rx(INT, "/sdk/v2/files"),
        callback=_recorder(creates, status=201, headers={"Location": "/sdk/v2/files/F2"}),
    )
    mock_wd.post(
        _rx(INT, "/sdk/v2/files"),
        callback=_recorder(creates, status=201, headers={"Location": "/sdk/v2/files/F3"}),
    )
    assert await client.ensure_folder("/Backups/Home Assistant/Würfel/") == "F3"
    metas = [_meta_from_multipart(kw["data"]) for _, kw in creates]
    assert metas == [
        {"parentID": "F1", "name": "Home Assistant", "mimeType": DIR_MIME},
        {"parentID": "F2", "name": "Würfel", "mimeType": DIR_MIME},
    ]
    assert creates[0][1]["headers"]["Content-Type"].startswith(
        "multipart/related; boundary="
    )
    q = parse_qs(urlparse(finds[0][0]).query)
    assert q["parentID"] == ["root"] and q["name"] == ["Backups"]


async def test_fallback_to_external(session, mock_wd):
    client = await _ready(session, mock_wd)
    err = aiohttp.ClientConnectorError(MagicMock(), OSError("dns"))
    mock_wd.get(_rx(INT, "/sdk/v2/filesSearch/parents"), exception=err)
    mock_wd.get(
        _rx(EXT, "/sdk/v2/filesSearch/parents"),
        payload={"files": [{"id": "a", "name": "A", "mimeType": DIR_MIME}]},
    )
    # Second call within 30 s goes straight to external (internal not mocked again).
    mock_wd.get(
        _rx(EXT, "/sdk/v2/filesSearch/parents"),
        payload={"files": []},
    )
    assert [f.id for f in await client.list_folder("root")] == ["a"]
    assert await client.list_folder("root") == []


async def test_connect_timeout_falls_back(session, mock_wd):
    client = await _ready(session, mock_wd)
    mock_wd.get(
        _rx(INT, "/sdk/v2/filesSearch/parents"),
        exception=aiohttp.ConnectionTimeoutError("connect"),
    )
    mock_wd.get(_rx(EXT, "/sdk/v2/filesSearch/parents"), payload={"files": []})
    assert await client.list_folder("root") == []


@pytest.mark.parametrize(
    "exc", [TimeoutError(), aiohttp.SocketTimeoutError("read")], ids=["total", "read"]
)
async def test_timeout_after_connect_does_not_resend_post(session, mock_wd, exc):
    client = await _ready(session, mock_wd)
    ext_calls: list = []
    mock_wd.post(_rx(INT, "/sdk/v2/files/resumable"), exception=exc)
    mock_wd.post(
        _rx(EXT, "/sdk/v2/files/resumable"),
        callback=_recorder(ext_calls, status=201, headers={"Location": "/x/NEW"}),
    )
    with pytest.raises(WDConnectionError):
        await client.upload("P1", "x.tar", _chunked(b"abc"))
    assert ext_calls == []


async def test_server_error_does_not_trigger_fallback(session, mock_wd):
    client = await _ready(session, mock_wd)
    mock_wd.get(_rx(INT, "/sdk/v2/filesSearch/parents"), status=502)
    with pytest.raises(WDConnectionError):
        await client.list_folder("root")


async def _chunked(*parts: bytes):
    for p in parts:
        yield p


def _mock_upload(mock_wd, writes, create_calls=None, fail_second=False):
    mock_wd.post(
        _rx(INT, "/sdk/v2/files/resumable"),
        callback=_recorder(
            create_calls if create_calls is not None else [],
            status=201,
            headers={"Location": "/sdk/v2/files/NEW"},
        ),
    )
    for i in range(3):
        status = 500 if (fail_second and i == 1) else 201
        mock_wd.post(
            _rx(INT, "/sdk/v2/files/NEW/resumable"),
            callback=_recorder(writes, status=status),
        )


def _summ(writes):
    out = []
    for url, kw in writes:
        q = parse_qs(urlparse(url).query)
        out.append((int(q["offset"][0]), kw["data"], q["done"][0]))
    return out


async def test_upload_chunks(session, mock_wd, monkeypatch):
    monkeypatch.setattr(api_mod, "CHUNK_SIZE", 4)
    client = await _ready(session, mock_wd)
    writes: list = []
    create: list = []
    _mock_upload(mock_wd, writes, create)
    fid = await client.upload("P1", "x.tar", _chunked(b"abc", b"defgh", b"ij"))
    assert fid == "NEW"
    assert _summ(writes) == [
        (0, b"abcd", "true"),
        (4, b"efgh", "true"),
        (8, b"ij", "true"),
    ]
    url, kw = create[0]
    assert parse_qs(urlparse(url).query) == {"done": ["true"]}
    assert _meta_from_multipart(kw["data"]) == {"parentID": "P1", "name": "x.tar"}


async def test_upload_exact_multiple(session, mock_wd, monkeypatch):
    monkeypatch.setattr(api_mod, "CHUNK_SIZE", 4)
    client = await _ready(session, mock_wd)
    writes: list = []
    _mock_upload(mock_wd, writes)
    await client.upload("P1", "x.tar", _chunked(b"abcdefgh"))
    assert _summ(writes) == [(0, b"abcd", "true"), (4, b"efgh", "true")]


async def test_upload_failure_cleans_up(session, mock_wd, monkeypatch):
    monkeypatch.setattr(api_mod, "CHUNK_SIZE", 4)
    client = await _ready(session, mock_wd)
    writes: list = []
    deletes: list = []
    _mock_upload(mock_wd, writes, fail_second=True)
    mock_wd.delete(
        _rx(INT, "/sdk/v2/files/NEW"), callback=_recorder(deletes, status=204)
    )
    with pytest.raises(WDConnectionError):
        await client.upload("P1", "x.tar", _chunked(b"abcdefghijkl"))
    assert len(deletes) == 1


async def test_upload_stream_error_cleans_up(session, mock_wd, monkeypatch):
    monkeypatch.setattr(api_mod, "CHUNK_SIZE", 4)
    client = await _ready(session, mock_wd)
    deletes: list = []
    _mock_upload(mock_wd, [])

    async def broken():
        yield b"abcdefg"
        raise RuntimeError("stream died")

    mock_wd.delete(
        _rx(INT, "/sdk/v2/files/NEW"), callback=_recorder(deletes, status=202)
    )
    with pytest.raises(RuntimeError):
        await client.upload("P1", "x.tar", broken())
    assert len(deletes) == 1


async def test_upload_cancel_cleans_up(session, mock_wd, monkeypatch):
    import asyncio

    monkeypatch.setattr(api_mod, "CHUNK_SIZE", 4)
    client = await _ready(session, mock_wd)
    deletes: list = []
    _mock_upload(mock_wd, [])

    async def hanging():
        yield b"abc"
        await asyncio.sleep(3600)

    mock_wd.delete(
        _rx(INT, "/sdk/v2/files/NEW"), callback=_recorder(deletes, status=202)
    )
    task = asyncio.ensure_future(client.upload("P1", "x.tar", hanging()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(deletes) == 1


async def test_download_streams(session, mock_wd):
    client = await _ready(session, mock_wd)
    mock_wd.get(_rx(INT, "/sdk/v3/files/F9/content"), body=b"hello world")
    data = b"".join([c async for c in await client.download("F9")])
    assert data == b"hello world"


async def test_download_not_found(session, mock_wd):
    client = await _ready(session, mock_wd)
    mock_wd.get(_rx(INT, "/sdk/v3/files/F9/content"), status=404)
    with pytest.raises(WDNotFoundError):
        await client.download("F9")


async def test_read_small(session, mock_wd):
    client = await _ready(session, mock_wd)
    mock_wd.get(_rx(INT, "/sdk/v3/files/F9/content"), body=b'{"a":1}')
    assert await client.read_small("F9") == b'{"a":1}'


async def test_delete_tolerates_404(session, mock_wd):
    client = await _ready(session, mock_wd)
    mock_wd.delete(_rx(INT, "/sdk/v2/files/F9"), status=404)
    mock_wd.delete(_rx(INT, "/sdk/v2/files/F9"), status=202)
    mock_wd.delete(_rx(INT, "/sdk/v2/files/F9"), status=204)
    for _ in range(3):
        await client.delete("F9")
