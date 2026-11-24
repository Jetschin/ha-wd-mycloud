"""Client for the (unofficial) WD My Cloud Off-Device API.

Intentionally free of Home Assistant imports so it can be reused standalone.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .const import CHUNK_SIZE, CLIENT_ID, CLIENT_SECRET, CONFIG_URL, SCOPE

TOKEN_EXPIRY_MARGIN = 60
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
DEVICE_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=5, sock_connect=5)
STREAM_TIMEOUT = aiohttp.ClientTimeout(
    total=None, connect=5, sock_connect=5, sock_read=300
)
EXTERNAL_STICKY_SECONDS = 30
DOWNLOAD_CHUNK = 1024 * 1024
DIR_MIME = "application/x.wd.dir"
FILE_FIELDS = "id,name,mimeType,size"


class WDError(Exception):
    """Base error for WD My Cloud API problems."""


class WDAuthError(WDError):
    """Credentials or refresh token rejected."""


class WDConnectionError(WDError):
    """Network problem, timeout or server-side (5xx) failure."""


class WDApiGoneError(WDError):
    """The WD config endpoint is gone or no longer has the expected shape."""


class WDNotFoundError(WDError):
    """The requested file does not exist."""


@dataclass
class WDDevice:
    """A My Cloud device of the user."""

    device_id: str
    name: str
    internal_url: str | None
    external_url: str | None


@dataclass
class WDFile:
    """A file or folder on the device."""

    id: str
    name: str
    is_dir: bool
    size: int


def _to_file(item: dict[str, Any]) -> WDFile:
    try:
        return WDFile(
            id=item["id"],
            name=item["name"],
            is_dir=item.get("mimeType") == DIR_MIME,
            size=int(item.get("size") or 0),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise WDError("Unexpected file entry from device") from err


def _multipart_body(meta: dict[str, str]) -> tuple[bytes, str]:
    """Build the multipart/related body used by the device API."""
    boundary = uuid.uuid4().hex
    payload = json.dumps(meta, ensure_ascii=False).encode()
    body = (
        f"--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/related; boundary={boundary}"


def _is_unreachable(err: WDConnectionError) -> bool:
    cause = err.__cause__
    # Only failures to establish the connection (incl. DNS and connect
    # timeouts) are safe to retry on another URL. A timeout after the
    # connection was made may have reached the device already, and re-sending
    # a non-idempotent POST elsewhere could duplicate it.
    return isinstance(
        cause, (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError)
    )


def _id_from_location(resp: aiohttp.ClientResponse) -> str:
    location = resp.headers.get("Location")
    file_id = urlparse(location).path.rstrip("/").rsplit("/", 1)[-1] if location else ""
    if not file_id:
        raise WDError("Device did not return the id of the new item")
    return file_id


def _decode_jwt(token: str) -> dict[str, Any]:
    """Decode the payload of a JWT without verifying the signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as err:
        raise WDError("Malformed token received from WD") from err
    if not isinstance(data, dict):
        raise WDError("Malformed token received from WD")
    return data


class WDClient:
    """Authenticated access to the WD My Cloud cloud services."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str | None = None,
        on_token_update: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._on_token_update = on_token_update
        self._access_token: str | None = None
        self._access_exp: float = 0
        self._user_id: str | None = None
        self._config: dict[str, Any] | None = None
        self._token_lock = asyncio.Lock()
        self._device: WDDevice | None = None
        self._external_until: float = 0
        # Target backup folder id; set by the integration after ensure_folder().
        self.folder_id: str | None = None

    @property
    def refresh_token(self) -> str | None:
        """Current refresh token."""
        return self._refresh_token

    @property
    def user_id(self) -> str | None:
        """WD user id (`sub` claim)."""
        return self._user_id

    async def _request_json(
        self, method: str, url: str, **kw: Any
    ) -> tuple[int, Any]:
        """Perform a request, returning (status, parsed JSON or None)."""
        try:
            async with self._session.request(
                method, url, timeout=REQUEST_TIMEOUT, **kw
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    body = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise WDConnectionError(f"Cannot reach {url}: {err}") from err
        return status, body

    async def _get_config(self) -> dict[str, Any]:
        """Fetch (and cache) the WD service configuration."""
        if self._config is not None:
            return self._config
        status, body = await self._request_json("GET", CONFIG_URL)
        if status in (404, 410):
            raise WDApiGoneError(f"WD config endpoint returned {status}")
        if status >= 500:
            raise WDConnectionError(f"WD config endpoint returned {status}")
        if status != 200:
            raise WDError(f"WD config endpoint returned {status}")
        try:
            urls = body["data"]["componentMap"]["cloud.service.urls"]
            auth_url = urls["service.auth0.url"]
            device_url = urls["service.device.url"]
        except (KeyError, TypeError) as err:
            raise WDApiGoneError("WD config has an unexpected format") from err
        self._config = {"auth_url": auth_url, "device_url": device_url}
        return self._config

    async def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        config = await self._get_config()
        status, body = await self._request_json(
            "POST", f"{config['auth_url']}/oauth/token", json=payload
        )
        if status in (404, 410):
            raise WDApiGoneError(f"WD token endpoint returned {status}")
        error = body.get("error") if isinstance(body, dict) else None
        if status in (401, 403) or error == "invalid_grant":
            raise WDAuthError(f"WD rejected the credentials ({status})")
        if status >= 500:
            raise WDConnectionError(f"WD auth service returned {status}")
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            raise WDError(f"Unexpected response from WD auth service ({status})")
        return body

    def _apply_tokens(self, body: dict[str, Any]) -> None:
        access = body["access_token"]
        claims = _decode_jwt(access)
        self._access_token = access
        self._access_exp = float(claims.get("exp") or time.time() + body.get("expires_in", 0))
        id_token = body.get("id_token")
        sub = _decode_jwt(id_token).get("sub") if id_token else claims.get("sub")
        if sub:
            self._user_id = sub
        new_refresh = body.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            if self._on_token_update is not None:
                self._on_token_update(new_refresh)

    async def login(self, email: str, password: str) -> str:
        """Log in with e-mail and password; return the refresh token."""
        body = await self._token_request(
            {
                "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
                "realm": "Username-Password-Authentication",
                "audience": "mycloud.com",
                "username": email,
                "password": password,
                "scope": SCOPE,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            }
        )
        if not body.get("refresh_token"):
            raise WDError("WD did not return a refresh token")
        self._apply_tokens(body)
        assert self._refresh_token is not None
        return self._refresh_token

    async def ensure_token(self) -> None:
        """Make sure a valid access token is available, refreshing if needed."""
        async with self._token_lock:
            if (
                self._access_token
                and self._access_exp - time.time() > TOKEN_EXPIRY_MARGIN
            ):
                return
            if not self._refresh_token:
                raise WDAuthError("No refresh token available")
            body = await self._token_request(
                {
                    "audience": "mycloud.com",
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                }
            )
            self._apply_tokens(body)

    async def _authed(
        self, method: str, url: str, **kw: Any
    ) -> aiohttp.ClientResponse:
        """Perform an authenticated request; the caller must release the response."""
        await self.ensure_token()
        headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {self._access_token}"}
        try:
            resp = await self._session.request(method, url, headers=headers, **kw)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise WDConnectionError(f"Cannot reach {url}: {err}") from err
        if resp.status in (401, 403):
            resp.release()
            raise WDAuthError(f"WD rejected the access token ({resp.status})")
        if resp.status >= 500:
            resp.release()
            raise WDConnectionError(f"WD service returned {resp.status}")
        return resp

    # --- device discovery -------------------------------------------------

    async def list_devices(self) -> list[WDDevice]:
        """List the user's My Cloud devices."""
        await self.ensure_token()
        config = await self._get_config()
        resp = await self._authed(
            "GET",
            f"{config['device_url']}/device/v1/user/{self._user_id}",
            timeout=DEVICE_TIMEOUT,
        )
        try:
            if resp.status != 200:
                raise WDError(f"Device list returned {resp.status}")
            body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise WDConnectionError(f"Cannot read device list: {err}") from err
        finally:
            resp.release()
        try:
            devices = []
            for item in body["data"]:
                net = item.get("network") or {}
                dns = net.get("internalDNSName")
                devices.append(
                    WDDevice(
                        device_id=item["deviceId"],
                        name=item.get("name") or item["deviceId"],
                        internal_url=f"https://{dns}" if dns else None,
                        external_url=net.get("externalURI") or None,
                    )
                )
        except (KeyError, TypeError, AttributeError) as err:
            raise WDError("Unexpected device list format") from err
        return devices

    def select_device(self, device: WDDevice) -> None:
        """Choose the device that file operations talk to."""
        self._device = device
        self._external_until = 0

    async def _device_request(
        self, method: str, path: str, **kw: Any
    ) -> aiohttp.ClientResponse:
        """Authenticated request to the device; internal URL first, then external.

        The caller must release the returned response.
        """
        dev = self._device
        if dev is None:
            raise WDError("No device selected")
        kw.setdefault("timeout", DEVICE_TIMEOUT)
        external_only = dev.internal_url is None or (
            dev.external_url is not None and time.monotonic() < self._external_until
        )
        if external_only:
            if dev.external_url is None:
                raise WDError("Device has no reachable URL")
            return await self._authed(method, dev.external_url + path, **kw)
        try:
            return await self._authed(method, dev.internal_url + path, **kw)
        except WDConnectionError as err:
            if dev.external_url is None or not _is_unreachable(err):
                raise
        resp = await self._authed(method, dev.external_url + path, **kw)
        self._external_until = time.monotonic() + EXTERNAL_STICKY_SECONDS
        return resp

    # --- file operations --------------------------------------------------

    async def list_folder(self, folder_id: str) -> list[WDFile]:
        """List the children of a folder (all pages)."""
        files: list[WDFile] = []
        page_token = ""
        while True:
            params = {"ids": folder_id, "fields": f"pageToken,{FILE_FIELDS}"}
            if page_token:
                params["pageToken"] = page_token
            resp = await self._device_request(
                "GET", "/sdk/v2/filesSearch/parents", params=params
            )
            try:
                if resp.status == 404:
                    raise WDNotFoundError(f"Folder {folder_id} not found")
                if resp.status != 200:
                    raise WDError(f"Listing folder returned {resp.status}")
                body = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                raise WDConnectionError(f"Cannot read folder listing: {err}") from err
            finally:
                resp.release()
            if not isinstance(body, dict):
                raise WDError("Unexpected folder listing format")
            files.extend(_to_file(item) for item in body.get("files") or [])
            page_token = body.get("pageToken") or ""
            if not page_token:
                return files

    async def find_child(self, parent_id: str, name: str) -> WDFile | None:
        """Find a child by exact name; None if it does not exist."""
        resp = await self._device_request(
            "GET",
            "/sdk/v2/filesSearch/parentAndName",
            params={"parentID": parent_id, "name": name, "fields": FILE_FIELDS},
        )
        try:
            if resp.status == 404:
                return None
            if resp.status != 200:
                raise WDError(f"Searching {name!r} returned {resp.status}")
            body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise WDConnectionError(f"Cannot read search result: {err}") from err
        finally:
            resp.release()
        if not isinstance(body, dict):
            raise WDError("Unexpected search result format")
        return _to_file(body)

    async def _create_folder(self, parent_id: str, name: str) -> str:
        body, ctype = _multipart_body(
            {"parentID": parent_id, "name": name, "mimeType": DIR_MIME}
        )
        resp = await self._device_request(
            "POST", "/sdk/v2/files", data=body, headers={"Content-Type": ctype}
        )
        try:
            if resp.status != 201:
                raise WDError(f"Creating folder {name!r} returned {resp.status}")
            return _id_from_location(resp)
        finally:
            resp.release()

    async def ensure_folder(self, path: str) -> str:
        """Return the id of a (nested) folder path, creating missing parts."""
        current = "root"
        for part in [p for p in path.strip("/").split("/") if p]:
            child = await self.find_child(current, part)
            if child is None:
                current = await self._create_folder(current, part)
            elif child.is_dir:
                current = child.id
            else:
                raise WDError(f"{part!r} exists but is not a folder")
        return current

    async def _write_block(self, file_id: str, offset: int, data: bytes) -> None:
        # Always done=true: live-verified that done=false on intermediate
        # blocks corrupts content on My Cloud Home (correct size, wrong bytes).
        resp = await self._device_request(
            "POST",
            f"/sdk/v2/files/{file_id}/resumable",
            params={"offset": str(offset), "done": "true"},
            data=data,
            timeout=STREAM_TIMEOUT,
        )
        try:
            if resp.status not in (200, 201, 202, 204):
                raise WDError(f"Writing to {file_id} returned {resp.status}")
        finally:
            resp.release()

    async def upload(
        self, parent_id: str, name: str, stream: AsyncIterator[bytes]
    ) -> str:
        """Create a file from a byte stream; returns the new file id.

        Every block is written with done=true. This is deliberate and
        live-verified: done=false on intermediate blocks produced a file of the
        correct size with wrong content (SHA-256 mismatch) on My Cloud Home.

        Memory use is bounded to about one CHUNK_SIZE block. On failure after
        the file was created, it is deleted again (best effort).
        """
        body, ctype = _multipart_body({"parentID": parent_id, "name": name})
        resp = await self._device_request(
            "POST",
            "/sdk/v2/files/resumable",
            params={"done": "true"},
            data=body,
            headers={"Content-Type": ctype},
        )
        try:
            if resp.status != 201:
                raise WDError(f"Creating file {name!r} returned {resp.status}")
            file_id = _id_from_location(resp)
        finally:
            resp.release()

        try:
            buf = bytearray()
            offset = 0
            async for piece in stream:
                buf.extend(piece)
                # Only flush when more data is known to follow, so no empty
                # trailing write is sent and a full final block is not split.
                while len(buf) > CHUNK_SIZE:
                    block = bytes(buf[:CHUNK_SIZE])
                    del buf[:CHUNK_SIZE]
                    await self._write_block(file_id, offset, block)
                    offset += len(block)
            if buf:
                await self._write_block(file_id, offset, bytes(buf))
        except BaseException:
            # Also on cancellation: shield the cleanup and bound its duration.
            try:
                await asyncio.wait_for(asyncio.shield(self.delete(file_id)), 15)
            except (Exception, asyncio.CancelledError):
                pass
            raise
        return file_id

    async def download(self, file_id: str) -> AsyncIterator[bytes]:
        """Start a download and return an iterator over the content.

        The request and status checks happen here (so a missing file raises
        WDNotFoundError immediately). The returned iterator releases the
        connection when exhausted or closed; if it is never consumed, the
        caller should `aclose()` it (or the connection is released on GC).
        """
        resp = await self._device_request(
            "GET", f"/sdk/v3/files/{file_id}/content", timeout=STREAM_TIMEOUT
        )
        if resp.status == 404:
            resp.release()
            raise WDNotFoundError(f"File {file_id} not found")
        if resp.status not in (200, 206):
            resp.release()
            raise WDError(f"Download returned {resp.status}")

        async def _iterate() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.content.iter_chunked(DOWNLOAD_CHUNK):
                    yield chunk
            except (aiohttp.ClientError, TimeoutError) as err:
                raise WDConnectionError(f"Download interrupted: {err}") from err
            finally:
                resp.release()

        return _iterate()

    async def read_small(self, file_id: str) -> bytes:
        """Read a whole (small) file into memory."""
        return b"".join([chunk async for chunk in await self.download(file_id)])

    async def delete(self, file_id: str) -> None:
        """Delete a file; an already missing file is fine."""
        resp = await self._device_request("DELETE", f"/sdk/v2/files/{file_id}")
        try:
            if resp.status not in (202, 204, 404):
                raise WDError(f"Deleting {file_id} returned {resp.status}")
        finally:
            resp.release()
