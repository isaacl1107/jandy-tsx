#!/usr/bin/env python3
"""Probe Jandy TCX pool light discovery + on/off over the Zodiac cloud.

Usage:
  export TCX_EMAIL='you@example.com'
  export TCX_PASSWORD='your-iaqualink-password'
  export TCX_SERIAL='RJEB01050120260091'   # optional; auto-picks first TCX

  python3 scripts/tcx_light_probe.py              # discover + turn ON
  python3 scripts/tcx_light_probe.py --off        # turn OFF
  python3 scripts/tcx_light_probe.py --discover   # no command, dump state only
  python3 scripts/tcx_light_probe.py --mock       # offline self-test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

import aiohttp  # noqa: E402


def _load_module(name: str, path: Path):
    """Load a component module without importing homeassistant via __init__."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_COMPONENT = ROOT / "custom_components" / "jandy_tcx"
# Preload const under the name api.py expects.
_load_module("custom_components.jandy_tcx.const", _COMPONENT / "const.py")
_api = _load_module("custom_components.jandy_tcx.api", _COMPONENT / "api.py")
TcxClient = _api.TcxClient


def _dump_aux(raw: dict) -> None:
    print("\n=== Aux circuits in reported state ===")
    found = False
    for key in sorted(raw):
        if not key.startswith("aux"):
            continue
        value = raw[key]
        if not isinstance(value, dict):
            continue
        found = True
        print(
            f"  {key}: st={value.get('st')} app={value.get('app')!r} "
            f"et={value.get('et')!r} ty={value.get('ty')!r} fr={value.get('fr')!r} "
            f"currClr={value.get('currClr')!r}"
        )
    if not found:
        print("  (none — REST shadow may be incomplete; WS Authorization needed)")


async def _run(args: argparse.Namespace) -> int:
    email = args.email or os.environ.get("TCX_EMAIL", "")
    password = args.password or os.environ.get("TCX_PASSWORD", "")
    serial = args.serial or os.environ.get("TCX_SERIAL", "")

    if not args.mock and (not email or not password):
        print(
            "Missing credentials. Set TCX_EMAIL and TCX_PASSWORD "
            "(and optional TCX_SERIAL), or pass --email/--password.",
            file=sys.stderr,
        )
        return 2

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = TcxClient(
            session,
            email or "mock@example.com",
            password or "mock",
            serial=serial or None,
            mock=args.mock,
        )
        print("Logging in…")
        await client.async_login()
        print("Login OK")

        if not client.serial:
            devices = await client.async_list_tcx_devices()
            print("TCX devices on account:")
            for device in devices:
                print(
                    f"  - {device.get('serial_number')} "
                    f"name={device.get('name')!r} type={device.get('device_type')!r}"
                )
            if not devices:
                print("No TCX controllers found.", file=sys.stderr)
                return 1
            client.serial = str(devices[0]["serial_number"])
            print(f"Using serial {client.serial}")

        print("Fetching REST shadow…")
        try:
            shadow = await client.async_get_shadow()
            reported = (shadow.get("state") or {}).get("reported") or {}
            print(f"REST reported keys: {sorted(reported)[:40]}")
        except Exception as err:  # noqa: BLE001
            print(f"REST shadow failed: {err}")

        print("Connecting websocket (wait for Authorization full state)…")
        try:
            await client.async_connect_ws(wait_for_auth=True)
            print(
                "WS connected; auth_event="
                f"{client._ws_auth_event.is_set()} "  # noqa: SLF001
                f"keys={sorted(client._reported)[:40]}"  # noqa: SLF001
            )
        except Exception as err:  # noqa: BLE001
            print(f"WS connect failed: {err}")

        state = client.get_state()
        _dump_aux(state.raw)
        print("\n=== Parsed light state ===")
        print(
            json.dumps(
                {
                    "serial": state.serial,
                    "online": state.online,
                    "light_available": state.light_available,
                    "light_key": state.light_key,
                    "light_name": state.light_name,
                    "light_on": state.light_on,
                    "light_is_color": state.light_is_color,
                    "light_color": state.light_color,
                },
                indent=2,
            )
        )

        if args.discover:
            return 0

        want_on = not args.off
        print(f"\nSending light {'ON' if want_on else 'OFF'}…")
        try:
            await client.async_set_light(want_on)
            print("Command sent (fire-and-forget over websocket).")
        except Exception as err:  # noqa: BLE001
            print(f"Command FAILED: {err}", file=sys.stderr)
            await client.async_close()
            return 1

        print("Waiting 3s for controller echo…")
        await asyncio.sleep(3)
        try:
            await client.async_get_shadow()
        except Exception:  # noqa: BLE001
            pass
        after = client.get_state()
        print("\n=== Light state after command ===")
        print(
            json.dumps(
                {
                    "light_key": after.light_key,
                    "light_on": after.light_on,
                    "raw_st": (after.raw.get(after.light_key or "") or {}).get("st")
                    if after.light_key
                    else None,
                },
                indent=2,
            )
        )
        _dump_aux(after.raw)

        ok = after.light_on is want_on
        print(
            "\nRESULT:",
            "PASS — reported state matches request"
            if ok
            else "INCONCLUSIVE/FAIL — reported state did not flip "
            "(command may still have worked if shadow is stale)",
        )
        await client.async_close()
        return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--serial", default="")
    parser.add_argument("--off", action="store_true", help="Turn light off")
    parser.add_argument(
        "--discover", action="store_true", help="Only dump state, no command"
    )
    parser.add_argument("--mock", action="store_true", help="Offline mock controller")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
