#!/usr/bin/env python3
"""Probe the Hoymiles station output power limit (read by default).

Background: a reduced AC output can come from more than one cloud setting. The
manufacturer's public web client (``static.hoymiles.com/cdncache/website/js``,
inspected 2026-11-10) has two drawers that touch it:

``PowerLimitDrawer`` (station level)
    write ``showCommand(8, {sid, power_limit, enable: 1})``
    active power 2-100 %, power factor -> action 47, reactive power -> action 48

``PowerControlDrawer`` (per device, behind an LCM)
    read  ``{action: 2006, dev_sn, dtu_sn, data: {mode}}``
    write ``{action: 2006, dev_sn, dtu_sn, data: {power_limit, mode}}``
    mode 1 = active power 0-110 %, 2 = power factor, 3 = reactive power

Both go through ``/pvm-ctl/api/0/dev/command/put`` and resolve through
``/pvm-ctl/api/0/dev/command/put_status``, where ``data.code`` is
0 = success, 1 = failed, 2 = still running, 3 = timeout.

This script reads, by default:

  * ``station_info.config.power_limit``  - station-level percentage
  * action 8 with ``{sid}``              - whether the station command answers
  * ``cfg_g_m`` type 515                 - configured upper limit, W
  * action 2006 mode 1 per microinverter - the per-device drawer value

``--write <percent>`` sends the station-level write. It defaults to off, and
writing the value that is already stored (e.g. ``--write 50``) is the safe way
to prove the write path works without changing anything physically.

Usage::

    pip install aiohttp argon2-cffi
    python3 scripts/probe_power_settings.py
    python3 scripts/probe_power_settings.py --write 50

``argon2-cffi`` is not optional: without it the Hoymiles v3 login only offers
unsalted hashing, which this account family rejects, and the login then fails
with "Argon2 support is unavailable for salted v3 authentication".
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import json
import logging
import os
import pathlib
import sys
import types
from typing import Any

import aiohttp

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
INTEGRATION_ROOT = REPO_ROOT / "custom_components" / "hoymiles_cloud"
PACKAGE_NAME = "custom_components.hoymiles_cloud"

CFG_G_M_PATH = "/pvm/api/0/station/setting/cfg_g_m"
COMMAND_PUT_PATH = "/pvm-ctl/api/0/dev/command/put"
COMMAND_STATUS_PATH = "/pvm-ctl/api/0/dev/command/put_status"

OUTPUT_LIMIT_CONFIG_TYPE = 515
ACTION_STATION_POWER_LIMIT = 8
ACTION_DEVICE_POWER_CONTROL = 2006
MODE_ACTIVE_POWER = 1

STATUS_RUNNING = 2


def _load_module(module_name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {module_name} from {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_integration():
    """Load the API client and constants without importing Home Assistant."""
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(INTEGRATION_ROOT)]
    sys.modules[PACKAGE_NAME] = package

    const = _load_module(f"{PACKAGE_NAME}.const", INTEGRATION_ROOT / "const.py")
    _load_module(f"{PACKAGE_NAME}.auth", INTEGRATION_ROOT / "auth.py")
    api_module = _load_module(
        f"{PACKAGE_NAME}.hoymiles_api",
        INTEGRATION_ROOT / "hoymiles_api.py",
    )
    return api_module.HoymilesAPI, const


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(
        description="Probe the Hoymiles station output power limit.",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Hoymiles account email. Defaults to HOYMILES_USERNAME.",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Hoymiles account password. Defaults to HOYMILES_PASSWORD.",
    )
    parser.add_argument(
        "--station",
        default=None,
        help="Only probe this station id.",
    )
    parser.add_argument(
        "--polls",
        type=int,
        default=20,
        help="Status polls to wait for an asynchronous command (1 s each).",
    )
    parser.add_argument(
        "--write",
        type=int,
        default=None,
        metavar="PERCENT",
        help=(
            "Send the station-level power limit write (2-100 %%). Off by default. "
            "Use the currently stored value for a no-op proof of the write path."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging of the API client.",
    )
    return parser


def _dump(label: str, payload: Any) -> None:
    print(f"\n--- {label} ---", flush=True)
    print(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        flush=True,
    )


def _job_from(raw: Any) -> tuple[Any, Any]:
    """Return the job id and access key from a command/put response."""
    data = raw.get("data") if isinstance(raw, dict) else None
    if isinstance(data, dict):
        return data.get("id") or data.get("job_id"), data.get("ak")
    if isinstance(data, (str, int)):
        return data, None
    return None, None


async def _command(
    api,
    const,
    payload: dict[str, Any],
    polls: int,
    label: str,
) -> Any:
    """Send one device command and follow the async job to its result."""
    print(f"\n>>> {label}", flush=True)
    raw = await api._post_json(f"{const.API_BASE_URL}{COMMAND_PUT_PATH}", payload)
    job_id, job_key = _job_from(raw)
    print(f"    put -> status={raw.get('status')} job={job_id}", flush=True)
    if job_id is None:
        _dump(f"{label}: put response", raw)
        return None

    for attempt in range(1, polls + 1):
        status = await api._post_json(
            f"{const.API_BASE_URL}{COMMAND_STATUS_PATH}",
            {"id": str(job_id), "ak": job_key},
        )
        body = status.get("data")
        code = body.get("code") if isinstance(body, dict) else None
        rate = body.get("rate") if isinstance(body, dict) else None
        rates = [
            item.get("rate")
            for item in (body.get("speed") or [])
            if isinstance(item, dict)
        ]
        print(f"    poll {attempt}: code={code} rate={rate} rates={rates}", flush=True)
        if code != STATUS_RUNNING:
            _dump(f"{label}: final status", status)
            return status
        await asyncio.sleep(1)

    print(f"    still code={STATUS_RUNNING} after {polls} polls", flush=True)
    return None


async def _fetch_microinverters(api, const, station_id: int) -> list[dict[str, Any]]:
    """Return the microinverter list with serials for one station."""
    response = await api._post_json(
        const.API_MICROINVERTERS_URL,
        {"sid": station_id, "page_size": 100, "page_num": 1},
    )
    data = response.get("data")
    if not isinstance(data, dict):
        return []
    return [item for item in data.get("list") or [] if isinstance(item, dict)]


def _print_station_config(details: dict[str, Any], label: str) -> str | None:
    """Print the station-level config block and return its power_limit."""
    config = details.get("config") or {}
    _dump(
        label,
        {
            "power_limit": config.get("power_limit"),
            "power_limit_pf": config.get("power_limit_pf"),
            "power_limit_re": config.get("power_limit_re"),
            "capacitor": details.get("capacitor"),
        },
    )
    return config.get("power_limit")


async def probe_station(
    api,
    const,
    station_id: str,
    station_name: str,
    polls: int,
    write_value: int | None,
) -> None:
    """Probe one station."""
    print(f"\n{'=' * 70}")
    print(f"station {station_id} - {station_name}")
    print("=" * 70)

    station_id_int = int(station_id)

    details = await api.get_station_details(station_id)
    _print_station_config(details, "station_info.config (before)")

    if write_value is not None:
        await _command(
            api,
            const,
            {
                "action": ACTION_STATION_POWER_LIMIT,
                "data": {
                    "sid": station_id_int,
                    "power_limit": write_value,
                    "enable": 1,
                },
            },
            polls,
            f"action {ACTION_STATION_POWER_LIMIT} WRITE power_limit={write_value}",
        )
        details = await api.get_station_details(station_id)
        _print_station_config(details, "station_info.config (after)")
        return

    await _command(
        api,
        const,
        {"action": ACTION_STATION_POWER_LIMIT, "data": {"sid": station_id_int}},
        polls,
        f"action {ACTION_STATION_POWER_LIMIT} read {{sid}}",
    )

    print(f"\n>>> cfg_g_m type {OUTPUT_LIMIT_CONFIG_TYPE}", flush=True)
    try:
        response = await api._post_json(
            f"{const.API_BASE_URL}{CFG_G_M_PATH}",
            {"sid": station_id_int, "type": OUTPUT_LIMIT_CONFIG_TYPE},
        )
        data = response.get("data") if isinstance(response, dict) else None
        block = (
            data.get(str(OUTPUT_LIMIT_CONFIG_TYPE)) if isinstance(data, dict) else None
        )
        _dump(
            f"cfg_g_m type {OUTPUT_LIMIT_CONFIG_TYPE} (configured upper limit, W)",
            block if block is not None else response,
        )
    except Exception as err:  # noqa: BLE001 - a probe should not abort
        print(f"FAILED: {err!r}", flush=True)

    microinverters = await _fetch_microinverters(api, const, station_id_int)
    _dump(
        "microinverters",
        [
            {
                "sn": item.get("sn"),
                "dtu_sn": item.get("dtu_sn"),
                "model": item.get("init_hard_no"),
            }
            for item in microinverters
        ],
    )

    for item in microinverters:
        dev_sn = item.get("sn")
        dtu_sn = item.get("dtu_sn") or dev_sn
        try:
            await _command(
                api,
                const,
                {
                    "action": ACTION_DEVICE_POWER_CONTROL,
                    "dev_sn": dev_sn,
                    "dtu_sn": dtu_sn,
                    "data": {"mode": MODE_ACTIVE_POWER},
                },
                polls,
                f"action {ACTION_DEVICE_POWER_CONTROL} mode {MODE_ACTIVE_POWER} "
                f"read for {dev_sn}",
            )
        except Exception as err:  # noqa: BLE001
            print(f"FAILED: {err!r}", flush=True)


def _require_argon2() -> bool:
    """Fail fast when the salted v3 login dependency is missing."""
    try:
        import argon2.low_level  # noqa: F401
    except ImportError:
        print(
            "Missing dependency: argon2-cffi.\n"
            "Without it the Hoymiles v3 login only offers unsalted hashing, and this\n"
            'account family rejects that with "Argon2 support is unavailable for\n'
            'salted v3 authentication".\n\n'
            "Install it and retry:\n"
            "    pip install argon2-cffi",
            file=sys.stderr,
        )
        return False
    return True


async def main() -> int:
    """Run the probe."""
    if not _require_argon2():
        return 2

    args = build_parser().parse_args()
    if args.write is not None and not 2 <= args.write <= 100:
        print("--write must be between 2 and 100 (%)", file=sys.stderr)
        return 2

    username = args.username or os.environ.get("HOYMILES_USERNAME")
    password = args.password or os.environ.get("HOYMILES_PASSWORD")

    if not username:
        username = input("Hoymiles username: ").strip()
    if not password:
        password = getpass.getpass("Hoymiles password: ")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    HoymilesAPI, const = load_integration()
    async with aiohttp.ClientSession() as session:
        api = HoymilesAPI(session, username, password)
        if not await api.authenticate():
            print(
                f"Authentication failed: {api.last_auth_error_key or 'unknown reason'}"
            )
            return 1

        stations = await api.get_stations()
        if not stations:
            print("No stations returned for this account.")
            return 1

        for station_id, station_name in stations.items():
            if args.station and str(args.station) != str(station_id):
                continue
            await probe_station(
                api,
                const,
                station_id,
                station_name,
                args.polls,
                args.write,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
