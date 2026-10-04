#!/usr/bin/env python3
"""Probe Jandy TCX pool light discovery + on/off over the Zodiac cloud.

By default this script TURNS THE POOL LIGHT ON after discovery succeeds.
Use --off to turn it off, or --discover to only inspect state.

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
        print("  (none — waiting for websocket Authorization / pib0)")


def _is_real_aux(value: dict | None) -> bool:
    """True when aux looks like device data, not an optimistic local stub."""
    if not isinstance(value, dict):
        return False
    return any(
        value.get(field) is not None
        for field in ("app", "et", "ty", "fr", "currClr", "cmdClr")
    )


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
        if not args.discover:
            print(
                "Mode:",
                "TURN LIGHT OFF" if args.off else "TURN LIGHT ON",
                "(after websocket discovery)",
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

        print("Fetching REST shadow (main only; aux comes from websocket)…")
        try:
            shadow = await client.async_get_shadow()
            reported = (shadow.get("state") or {}).get("reported") or {}
            print(f"REST reported keys: {sorted(reported)}")
            print(f"equipment flags: {reported.get('equipment')!r}")
        except Exception as err:  # noqa: BLE001
            print(f"REST shadow failed: {err}")

        print("Connecting websocket (wait for Authorization full state)…")
        try:
            await client.async_connect_ws(wait_for_auth=True)
            print(
                "WS connected; auth_event="
                f"{client._ws_auth_event.is_set()} "  # noqa: SLF001
                f"keys={sorted(client._reported)}"  # noqa: SLF001
            )
            if client._ws_debug_frames:  # noqa: SLF001
                print("\n=== WS frames seen ===")
                print(json.dumps(client._ws_debug_frames, indent=2))  # noqa: SLF001
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
            await client.async_close()
            return 0 if state.light_available else 1

        if not state.light_available or not state.light_key:
            print(
                "\nRESULT: FAIL — light circuit not discovered from websocket "
                "Authorization (expected aux0 POOL_LT on this controller).",
                file=sys.stderr,
            )
            await client.async_close()
            return 1

        want_on = not args.off
        aux_key = state.light_key
        print(f"\nSending light {'ON' if want_on else 'OFF'} via {aux_key}…")
        print(
            "(Requires a websocket echo of aux.st — local optimistic updates "
            "no longer count as success.)"
        )
        try:
            await client.async_set_light(want_on)
            print("Controller echoed the new light state over websocket.")
        except Exception as err:  # noqa: BLE001
            print(f"Command FAILED: {err}", file=sys.stderr)
            print("\n=== WS frames after failed command ===")
            print(json.dumps(client._ws_debug_frames, indent=2))  # noqa: SLF001
            _dump_aux(client.get_state().raw)
            await client.async_close()
            return 1

        after = client.get_state()
        raw_aux = after.raw.get(aux_key)
        print("\n=== Light state after confirmed command ===")
        print(
            json.dumps(
                {
                    "light_key": after.light_key,
                    "light_on": after.light_on,
                    "raw_st": (raw_aux or {}).get("st")
                    if isinstance(raw_aux, dict)
                    else None,
                    "raw_fr": (raw_aux or {}).get("fr")
                    if isinstance(raw_aux, dict)
                    else None,
                    "ws_frames_total": len(client._ws_debug_frames),  # noqa: SLF001
                },
                indent=2,
            )
        )
        _dump_aux(after.raw)
        print(
            "\nRESULT: PASS — cloud echoed the new state. "
            "Confirm the physical light also changed."
        )
        await client.async_close()
        return 0


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
