#!/usr/bin/env python3
"""Live check of the WD My Cloud upload semantics (manual, run by the owner).

Logs in, uploads 20 MiB of random data to HA_Backup/_wd_mycloud_test, downloads
it again, compares SHA-256 and cleans up. Refuses to run if that test folder
already exists (it only ever deletes what this run created). Prints "RESULT: ...".
Works with the repo layout (scripts/ next to custom_components/) or with
api.py, const.py and live_check.py together in one folder.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import importlib
import os
import sys
import types
from pathlib import Path

try:
    import aiohttp
except ImportError:
    print("Bitte zuerst installieren: python3 -m pip install --user aiohttp")
    sys.exit(1)

TEST_FOLDER = "HA_Backup/_wd_mycloud_test"
TEST_SIZE = 20 * 1024 * 1024


def _find_integration_dir() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "custom_components" / "wd_mycloud", here):
        if (cand / "api.py").is_file() and (cand / "const.py").is_file():
            return cand
    print("api.py / const.py nicht gefunden.")
    sys.exit(1)


def _load_modules():
    """Import const/api without running the integration's __init__.py."""
    pkg = types.ModuleType("wd_mycloud")
    pkg.__path__ = [str(_find_integration_dir())]
    sys.modules["wd_mycloud"] = pkg
    const = importlib.import_module("wd_mycloud.const")
    api = importlib.import_module("wd_mycloud.api")
    return const, api


const, api = _load_modules()


async def _chunks(data: bytes, size: int = 1024 * 1024):
    for i in range(0, len(data), size):
        yield data[i : i + size]


async def _verify(client, file_id: str, data: bytes) -> str | None:
    """Download and compare; return None if OK, else a failure description."""
    got = bytearray()
    sha = hashlib.sha256()
    async for chunk in await client.download(file_id):
        got.extend(chunk)
        sha.update(chunk)
    if len(got) == len(data):
        print("SIZE OK")
    else:
        return f"Größe stimmt nicht ({len(got)} statt {len(data)} Bytes)"
    if sha.hexdigest() == hashlib.sha256(data).hexdigest():
        print("HASH OK")
    else:
        return "SHA-256 stimmt nicht"
    return None


async def run() -> str:
    email = input("WD-E-Mail: ").strip()
    password = getpass.getpass("WD-Passwort: ")
    data = os.urandom(TEST_SIZE)
    created: list[str] = []
    folder_id = None  # only set if THIS run created the test folder
    step = "Anmeldung"
    async with aiohttp.ClientSession() as session:
        client = api.WDClient(session)
        try:
            await client.login(email, password)
            step = "Geräteliste"
            devices = await client.list_devices()
            if not devices:
                return "RESULT: FAIL – Geräteliste (kein Gerät gefunden)"
            client.select_device(devices[0])
            print(f"Gerät: {devices[0].name}")
            step = "Ordner anlegen"
            parent_path, _, test_name = TEST_FOLDER.rpartition("/")
            parent_id = await client.ensure_folder(parent_path)
            if await client.find_child(parent_id, test_name) is not None:
                return (
                    f"RESULT: FAIL – Der Ordner {TEST_FOLDER} existiert bereits "
                    "und wurde nicht angefasst. Bitte prüfen und ggf. manuell "
                    "entfernen, dann erneut starten."
                )
            folder_id = await client.ensure_folder(TEST_FOLDER)

            step = "Upload"
            file_id = await client.upload(folder_id, "test.bin", _chunks(data))
            created.append(file_id)
            step = "Download/Vergleich"
            err = await _verify(client, file_id, data)
            if err is None:
                return "RESULT: PASS"
            return f"RESULT: FAIL – {step} ({err})"
        except Exception as exc:  # noqa: BLE001 - always report a RESULT line
            return f"RESULT: FAIL – {step} ({type(exc).__name__}: {exc})"
        finally:
            print("Aufräumen ...")
            for file_id in created:
                try:
                    await client.delete(file_id)
                except Exception as exc:  # noqa: BLE001 - best effort cleanup
                    print(f"Datei {file_id} nicht gelöscht: {exc}")
            if folder_id:
                try:
                    await client.delete(folder_id)
                except Exception as exc:  # noqa: BLE001
                    print(f"Testordner nicht gelöscht: {exc}")


def main() -> None:
    try:
        result = asyncio.run(run())
    except KeyboardInterrupt:
        result = "RESULT: FAIL – abgebrochen"
    print(result)
    sys.exit(0 if result == "RESULT: PASS" else 1)


if __name__ == "__main__":
    main()
