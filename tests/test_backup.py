"""Tests for the WD My Cloud backup agent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgentError,
    BackupNotFound,
)
from homeassistant.components.backup import DOMAIN as BACKUP_DOMAIN
from homeassistant.components.backup.const import DATA_MANAGER as BACKUP_DATA_MANAGER
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wd_mycloud.api import (
    WDAuthError,
    WDConnectionError,
    WDDevice,
    WDFile,
    WDNotFoundError,
)
from custom_components.wd_mycloud.backup import (
    async_get_backup_agents,
    suggested_filenames,
)
from custom_components.wd_mycloud.const import (
    CONF_DEVICE_ID,
    CONF_FOLDER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

DEV1 = WDDevice("dev1", "My Cloud Home", "https://i1", "https://e1")

BACKUP = AgentBackup(
    addons=[],
    backup_id="abc123",
    date="2026-01-02T03:04:05.000000+00:00",
    database_included=True,
    extra_metadata={},
    folders=[],
    homeassistant_included=True,
    homeassistant_version="2026.1.0",
    name="Test backup",
    protected=False,
    size=11,
)


class FakeClient:
    """In-memory stand-in for WDClient."""

    def __init__(self) -> None:
        self.folder_id = "fid"
        self.files: dict[str, tuple[WDFile, bytes]] = {}
        self.calls: list[tuple] = []
        self._n = 0
        self.fail_with: Exception | None = None

    def add(self, name: str, data: bytes, is_dir: bool = False) -> str:
        self._n += 1
        fid = f"f{self._n}"
        self.files[fid] = (WDFile(fid, name, is_dir, len(data)), data)
        return fid

    async def ensure_token(self):
        pass

    async def list_devices(self):
        return [DEV1]

    def select_device(self, device):
        pass

    async def ensure_folder(self, path):
        return "fid"

    async def list_folder(self, folder_id):
        self.calls.append(("list_folder", folder_id))
        if self.fail_with:
            raise self.fail_with
        return [f for f, _ in self.files.values()]

    async def find_child(self, parent_id, name):
        return next((f for f, _ in self.files.values() if f.name == name), None)

    async def upload(self, parent_id, name, stream):
        self.calls.append(("upload", parent_id, name))
        if self.fail_with:
            raise self.fail_with
        data = b""
        try:
            async for chunk in stream:
                data += chunk
        except Exception:
            self.calls.append(("cleanup", name))
            raise
        return self.add(name, data)

    async def download(self, file_id):
        self.calls.append(("download", file_id))
        if file_id not in self.files:
            raise WDNotFoundError(file_id)
        data = self.files[file_id][1]

        async def _it():
            yield data[:5]
            yield data[5:]

        return _it()

    async def read_small(self, file_id):
        return b"".join([c async for c in await self.download(file_id)])

    async def delete(self, file_id):
        self.calls.append(("delete", file_id))
        self.files.pop(file_id, None)


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
async def entry(hass, fake):
    assert await async_setup_component(hass, BACKUP_DOMAIN, {})
    e = MockConfigEntry(
        domain=DOMAIN,
        title="My NAS",
        data={CONF_REFRESH_TOKEN: "R1", CONF_DEVICE_ID: "dev1", CONF_FOLDER: "HA_Backup"},
    )
    e.add_to_hass(hass)
    with patch("custom_components.wd_mycloud.WDClient", return_value=fake):
        assert await hass.config_entries.async_setup(e.entry_id)
        await hass.async_block_till_done()
    return e


@pytest.fixture
async def agent(hass, entry):
    agents = await async_get_backup_agents(hass)
    assert len(agents) == 1
    return agents[0]


def _store_backup(fake: FakeClient, backup: AgentBackup = BACKUP) -> None:
    tar, meta = suggested_filenames(backup)
    fake.add(tar, b"hello world")
    import json

    fake.add(meta, json.dumps(backup.as_dict()).encode())


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


async def test_agents_listed(hass, entry, agent):
    assert agent.domain == DOMAIN
    assert agent.name == "My NAS"
    assert agent.unique_id == entry.entry_id
    manager = hass.data[BACKUP_DATA_MANAGER]
    assert f"{DOMAIN}.{entry.entry_id}" in manager.backup_agents


async def test_upload_writes_tar_then_metadata(agent, fake):
    async def open_stream():
        return _stream(b"hello ", b"world")

    await agent.async_upload_backup(open_stream=open_stream, backup=BACKUP)

    tar, meta = suggested_filenames(BACKUP)
    uploads = [c for c in fake.calls if c[0] == "upload"]
    assert [u[2] for u in uploads] == [tar, meta]
    assert all(u[1] == "fid" for u in uploads)
    by_name = {f.name: d for f, d in fake.files.values()}
    assert by_name[tar] == b"hello world"
    assert [b.backup_id for b in await agent.async_list_backups()] == ["abc123"]
    assert (await agent.async_list_backups())[0] == BACKUP


async def test_upload_stream_error(agent, fake):
    async def bad() -> AsyncIterator[bytes]:
        yield b"partial"
        raise OSError("stream broke")

    async def open_stream():
        return bad()

    with pytest.raises(BackupAgentError):
        await agent.async_upload_backup(open_stream=open_stream, backup=BACKUP)

    names = [c[2] for c in fake.calls if c[0] == "upload"]
    assert names == [suggested_filenames(BACKUP)[0]]  # metadata never attempted
    assert ("cleanup", names[0]) in fake.calls
    assert not fake.files


async def test_upload_metadata_failure_removes_tar(agent, fake):
    orig = fake.upload

    async def flaky(parent_id, name, stream):
        if name.endswith(".metadata.json"):
            raise WDConnectionError("down")
        return await orig(parent_id, name, stream)

    fake.upload = flaky

    async def open_stream():
        return _stream(b"x")

    with pytest.raises(BackupAgentError):
        await agent.async_upload_backup(open_stream=open_stream, backup=BACKUP)
    assert not fake.files


async def test_list_ignores_foreign_and_broken(agent, fake):
    fake.add("notes.txt", b"hi")
    fake.add("bad.metadata.json", b"{oops")
    fake.add("bad.tar", b"x")
    fake.add("orphan.metadata.json", b'{"backup_id": "zzz"}')
    fake.add("sub", b"", is_dir=True)
    _store_backup(fake)

    backups = await agent.async_list_backups()
    assert [b.backup_id for b in backups] == ["abc123"]


async def test_list_orphan_valid_metadata_without_tar(agent, fake):
    import json

    fake.add("lonely.metadata.json", json.dumps(BACKUP.as_dict()).encode())
    assert await agent.async_list_backups() == []


async def test_list_uses_cache(agent, fake):
    _store_backup(fake)
    await agent.async_list_backups()
    n = len([c for c in fake.calls if c[0] == "list_folder"])
    await agent.async_get_backup("abc123")
    assert len([c for c in fake.calls if c[0] == "list_folder"]) == n


async def test_get_unknown_raises_not_found(agent, fake):
    with pytest.raises(BackupNotFound):
        await agent.async_get_backup("nope")
    with pytest.raises(BackupNotFound):
        await agent.async_download_backup("nope")
    with pytest.raises(BackupNotFound):
        await agent.async_delete_backup("nope")


async def test_download_streams_bytes(agent, fake):
    _store_backup(fake)
    stream = await agent.async_download_backup("abc123")
    assert b"".join([c async for c in stream]) == b"hello world"


async def test_delete_removes_both_files(agent, fake):
    _store_backup(fake)
    fake.add("notes.txt", b"keep")
    await agent.async_delete_backup("abc123")
    assert [f.name for f, _ in fake.files.values()] == ["notes.txt"]
    deletes = [c for c in fake.calls if c[0] == "delete"]
    assert len(deletes) == 2
    assert await agent.async_list_backups() == []


async def test_auth_error_triggers_reauth(hass, entry, agent, fake):
    fake.fail_with = WDAuthError("revoked")
    with pytest.raises(BackupAgentError, match="Authentication"):
        await agent.async_list_backups()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_connection_error_is_agent_error(agent, fake):
    fake.fail_with = WDConnectionError("down")
    with pytest.raises(BackupAgentError):
        await agent.async_list_backups()
    fake.fail_with = TimeoutError()
    with pytest.raises(BackupAgentError):
        await agent.async_list_backups()


async def test_download_missing_file_maps_to_not_found(agent, fake):
    _store_backup(fake)
    await agent.async_list_backups()
    fake.files.clear()
    with pytest.raises(BackupNotFound):
        await agent.async_download_backup("abc123")


async def test_listener_registration(hass):
    from custom_components.wd_mycloud import DATA_BACKUP_AGENT_LISTENERS
    from custom_components.wd_mycloud.backup import (
        async_register_backup_agents_listener,
    )

    calls = []
    unsub = async_register_backup_agents_listener(hass, listener=lambda: calls.append(1))
    assert len(hass.data[DATA_BACKUP_AGENT_LISTENERS]) == 1
    unsub()
    assert DATA_BACKUP_AGENT_LISTENERS not in hass.data


async def test_upload_refuses_existing_name(agent, fake):
    tar, meta = suggested_filenames(BACKUP)
    fake.add(tar, b"foreign")

    async def open_stream():
        return _stream(b"x")

    with pytest.raises(BackupAgentError, match="already exists"):
        await agent.async_upload_backup(open_stream=open_stream, backup=BACKUP)
    assert not [c for c in fake.calls if c[0] in ("upload", "delete")]
    assert [d for _, d in fake.files.values()] == [b"foreign"]


async def test_upload_refuses_existing_metadata_name(agent, fake):
    _, meta = suggested_filenames(BACKUP)
    fake.add(meta, b"foreign")

    async def open_stream():
        return _stream(b"x")

    with pytest.raises(BackupAgentError, match="already exists"):
        await agent.async_upload_backup(open_stream=open_stream, backup=BACKUP)
    assert not [c for c in fake.calls if c[0] in ("upload", "delete")]


async def test_list_skips_duplicate_names(agent, fake):
    _store_backup(fake)
    tar, _ = suggested_filenames(BACKUP)
    fake.add(tar, b"foreign duplicate")
    assert await agent.async_list_backups() == []
    with pytest.raises(BackupNotFound):
        await agent.async_delete_backup("abc123")
    assert not [c for c in fake.calls if c[0] == "delete"]


async def test_upload_size_mismatch_removes_tar(agent, fake):
    orig = fake.upload

    async def truncating(parent_id, name, stream):
        fid = await orig(parent_id, name, stream)
        wd, data = fake.files[fid]
        fake.files[fid] = (WDFile(fid, name, False, len(data) - 1), data)
        return fid

    fake.upload = truncating

    async def open_stream():
        return _stream(b"hello ", b"world")

    with pytest.raises(BackupAgentError, match="verification"):
        await agent.async_upload_backup(open_stream=open_stream, backup=BACKUP)

    tar, meta = suggested_filenames(BACKUP)
    assert [f.name for f, _ in fake.files.values()] == []
    assert [c[2] for c in fake.calls if c[0] == "upload"] == [tar]
    assert any(c[0] == "delete" for c in fake.calls)


async def test_list_skips_oversized_metadata(agent, fake):
    tar, meta = suggested_filenames(BACKUP)
    fake.add(tar, b"hello world")
    fid = fake.add(meta, b"{}")
    fake.files[fid] = (WDFile(fid, meta, False, 2 * 1024 * 1024), b"{}")
    assert await agent.async_list_backups() == []
    assert not any(c == ("download", fid) for c in fake.calls)


async def test_invalidate_during_listing_is_not_cached(agent, fake):
    _store_backup(fake)
    orig = fake.read_small

    async def racing(file_id):
        agent._invalidate_cache()  # e.g. an upload finished meanwhile
        return await orig(file_id)

    fake.read_small = racing
    assert len(await agent.async_list_backups()) == 1
    fake.read_small = orig
    n = len([c for c in fake.calls if c[0] == "list_folder"])
    await agent.async_list_backups()
    assert len([c for c in fake.calls if c[0] == "list_folder"]) == n + 1
