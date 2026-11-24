"""Backup agent for WD My Cloud."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from functools import wraps
import logging
import time
from typing import Any, Concatenate

import aiohttp

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    suggested_filename,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.json import json_dumps
from homeassistant.util.json import json_loads_object

from . import DATA_BACKUP_AGENT_LISTENERS, WDConfigEntry
from .api import WDAuthError, WDClient, WDError, WDFile, WDNotFoundError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CACHE_TTL = 300
METADATA_SUFFIX = ".metadata.json"
MAX_METADATA_SIZE = 1024 * 1024


@dataclass
class _CachedBackup:
    """A backup with the remote files that belong to it."""

    backup: AgentBackup
    tar: WDFile
    metadata: WDFile


async def async_get_backup_agents(hass: HomeAssistant) -> list[BackupAgent]:
    """Return a list of backup agents."""
    entries: list[WDConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    return [WDMyCloudBackupAgent(hass, entry) for entry in entries]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    """Register a listener to be called when agents are added or removed."""
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


def handle_backup_errors[_R, **P](
    func: Callable[Concatenate[WDMyCloudBackupAgent, P], Coroutine[Any, Any, _R]],
) -> Callable[Concatenate[WDMyCloudBackupAgent, P], Coroutine[Any, Any, _R]]:
    """Map client errors to backup errors."""

    @wraps(func)
    async def wrapper(
        self: WDMyCloudBackupAgent, *args: P.args, **kwargs: P.kwargs
    ) -> _R:
        try:
            return await func(self, *args, **kwargs)
        except WDAuthError as err:
            self._entry.async_start_reauth(self._hass)
            raise BackupAgentError("Authentication error") from err
        except WDNotFoundError as err:
            raise BackupNotFound(f"Backup file not found: {err}") from err
        except WDError as err:
            _LOGGER.debug("Full error: %s", err, exc_info=True)
            raise BackupAgentError(f"Backup operation failed: {err}") from err
        except TimeoutError as err:
            _LOGGER.error("Error during backup in %s: Timeout", func.__name__)
            raise BackupAgentError("Backup operation timed out") from err
        except (aiohttp.ClientError, OSError) as err:
            _LOGGER.debug("Full error: %s", err, exc_info=True)
            raise BackupAgentError(f"Backup operation failed: {err}") from err

    return wrapper


def suggested_filenames(backup: AgentBackup) -> tuple[str, str]:
    """Return the suggested filenames for the backup and metadata."""
    base_name = suggested_filename(backup).rsplit(".", 1)[0]
    return f"{base_name}.tar", f"{base_name}{METADATA_SUFFIX}"


class WDMyCloudBackupAgent(BackupAgent):
    """Backup agent storing backups in a folder on a WD My Cloud Home."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: WDConfigEntry) -> None:
        """Initialize the agent."""
        super().__init__()
        self._hass = hass
        self._entry = entry
        self.name = entry.title
        self.unique_id = entry.entry_id
        self._cache: dict[str, _CachedBackup] = {}
        self._cache_expires = 0.0
        self._cache_generation = 0

    @property
    def _client(self) -> WDClient:
        return self._entry.runtime_data

    def _invalidate_cache(self) -> None:
        self._cache_expires = 0.0
        # A listing already in flight must not store its (stale) result.
        self._cache_generation += 1

    @property
    def _folder_id(self) -> str:
        folder_id = self._client.folder_id
        if folder_id is None:
            raise BackupAgentError("Backup folder is not available")
        return folder_id

    @handle_backup_errors
    async def async_download_backup(
        self, backup_id: str, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        """Download a backup file."""
        cached = await self._find(backup_id)
        return await self._client.download(cached.tar.id)

    @handle_backup_errors
    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        **kwargs: Any,
    ) -> None:
        """Upload a backup, then its metadata file."""
        tar_name, meta_name = suggested_filenames(backup)
        folder_id = self._folder_id
        self._invalidate_cache()

        # Never write next to a file we did not create: refuse on a name clash.
        for name in (tar_name, meta_name):
            if await self._client.find_child(folder_id, name) is not None:
                raise BackupAgentError(
                    f"A file named {name} already exists in the backup folder"
                )

        # The client removes a partial file itself if the stream fails.
        sent = 0

        async def _counted(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
            nonlocal sent
            async for chunk in stream:
                sent += len(chunk)
                yield chunk

        tar_id = await self._client.upload(
            folder_id, tar_name, _counted(await open_stream())
        )

        # Verify the stored size before the backup becomes visible.
        remote = await self._remove_if_unverified(folder_id, tar_name, tar_id, sent)
        _LOGGER.debug("Uploaded backup %s (%s bytes)", tar_name, remote)

        async def _metadata() -> AsyncIterator[bytes]:
            yield json_dumps(backup.as_dict()).encode()

        try:
            await self._client.upload(folder_id, meta_name, _metadata())
        except BaseException:
            # Without metadata the backup would be invisible; do not leave it.
            try:
                await self._client.delete(tar_id)
            except WDError:
                _LOGGER.debug("Could not remove %s after failure", tar_name)
            raise
        _LOGGER.debug("Uploaded metadata %s", meta_name)
        self._invalidate_cache()

    async def _remove_if_unverified(
        self, folder_id: str, tar_name: str, tar_id: str, expected: int
    ) -> int:
        """Check the uploaded tar has the streamed size; delete it if not."""
        try:
            remote = await self._client.find_child(folder_id, tar_name)
        except BaseException:
            await self._discard(tar_id, tar_name)
            raise
        if remote is not None and remote.size == expected:
            return remote.size
        await self._discard(tar_id, tar_name)
        found = "missing" if remote is None else f"{remote.size} bytes"
        raise BackupAgentError(
            f"Upload verification failed for {tar_name}: "
            f"expected {expected} bytes, remote file is {found}"
        )

    async def _discard(self, file_id: str, name: str) -> None:
        """Best-effort removal of a file this agent just created."""
        try:
            await self._client.delete(file_id)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not remove %s after failure", name)

    @handle_backup_errors
    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        """Delete a backup (tar first, then metadata)."""
        cached = await self._find(backup_id)
        try:
            await self._client.delete(cached.tar.id)
            await self._client.delete(cached.metadata.id)
        finally:
            self._invalidate_cache()
        _LOGGER.debug("Deleted backup %s", cached.tar.name)

    @handle_backup_errors
    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """List backups."""
        return [c.backup for c in (await self._list_cached()).values()]

    @handle_backup_errors
    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        """Return a backup."""
        return (await self._find(backup_id)).backup

    async def _list_cached(self) -> dict[str, _CachedBackup]:
        """Return backups found in the folder, cached for CACHE_TTL seconds."""
        if time.monotonic() < self._cache_expires:
            return self._cache

        generation = self._cache_generation
        files = await self._client.list_folder(self._folder_id)
        counts: dict[str, int] = {}
        for f in files:
            if not f.is_dir:
                counts[f.name] = counts.get(f.name, 0) + 1
        by_name = {f.name: f for f in files if not f.is_dir}
        found: dict[str, _CachedBackup] = {}
        for name, meta_file in by_name.items():
            if not name.endswith(METADATA_SUFFIX):
                continue
            tar_name = f"{name[: -len(METADATA_SUFFIX)]}.tar"
            tar_file = by_name.get(tar_name)
            if tar_file is None:
                continue
            if counts[name] > 1 or counts[tar_name] > 1:
                _LOGGER.debug("Ignoring %s: ambiguous duplicate file names", name)
                continue
            if meta_file.size > MAX_METADATA_SIZE:
                _LOGGER.debug("Ignoring %s: metadata file too large", name)
                continue
            try:
                raw = await self._client.read_small(meta_file.id)
            except WDNotFoundError:
                continue
            try:
                backup = AgentBackup.from_dict(json_loads_object(raw))
            except Exception as err:  # noqa: BLE001 - any foreign/broken file is skipped
                _LOGGER.debug("Ignoring %s: %s", name, err)
                continue
            found[backup.backup_id] = _CachedBackup(backup, tar_file, meta_file)

        if generation == self._cache_generation:
            self._cache = found
            self._cache_expires = time.monotonic() + CACHE_TTL
        return found

    async def _find(self, backup_id: str) -> _CachedBackup:
        if cached := (await self._list_cached()).get(backup_id):
            return cached
        raise BackupNotFound(f"Backup {backup_id} not found")
