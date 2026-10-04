#!/usr/bin/env python3
"""Diagnose / debug Jandy TCX filter-pump control over the Zodiac cloud.

Default mode is READ-ONLY (safe): dump REST vs websocket pump fields and the
HA-parsed view. Use --on / --off to send a real command with full debug of
every websocket frame and timing (requires --i-know).

--verify-echo re-sends the *current* st (no intended state change) to prove
the command envelope works without starting/stopping the motor.

Usage:
  export TCX_EMAIL='you@example.com'
  export TCX_PASSWORD='your-iaqualink-password'
  export TCX_SERIAL='RJEB01050120260091'   # optional

  python3 scripts/tcx_pump_probe.py -v
  python3 scripts/tcx_pump_probe.py --verify-echo -v
  python3 scripts/tcx_pump_probe.py --on --i-know -v
  python3 scripts/tcx_pump_probe.py --off --i-know -v
  python3 scripts/tcx_pump_probe.py --mock
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util  # noqa: E402

import aiohttp  # noqa: E402

_LOGGER = logging.getLogger("tcx_pump_probe")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_COMPONENT = ROOT / "custom_components" / "jandy_tcx"
_const = _load_module("custom_components.jandy_tcx.const", _COMPONENT / "const.py")
_api = _load_module("custom_components.jandy_tcx.api", _COMPONENT / "api.py")
TcxClient = _api.TcxClient
NAMESPACE_FILTRATION = _const.NAMESPACE_FILTRATION
NAMESPACE_TCX = _const.NAMESPACE_TCX
ACTION_SET_FILTER_PUMP_STATE = _const.ACTION_SET_FILTER_PUMP_STATE
ACTION_SET_STATE = _const.ACTION_SET_STATE
SERVICE_STATE_CONTROLLER = _const.SERVICE_STATE_CONTROLLER


def _obj(raw: dict, key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _st(obj: dict[str, Any]) -> int | None:
    if "st" not in obj:
        return None
    try:
        return int(obj.get("st") or 0)
    except (TypeError, ValueError):
        return None


def _dump_pump_block(title: str, raw: dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    for key in ("filt0", "pool", "ecm0"):
        obj = _obj(raw, key)
        if not obj:
            print(f"  {key}: (missing)")
            continue
        print(
            f"  {key}: st={obj.get('st')!r} en={obj.get('en')!r} "
            f"fr={obj.get('fr')!r} app={obj.get('app')!r} et={obj.get('et')!r}"
        )
        if key == "ecm0":
            print(
                f"         cmdSpd={obj.get('cmdSpd')!r} reqSpd={obj.get('reqSpd')!r} "
                f"minSpd={obj.get('minSpd')!r} maxSpd={obj.get('maxSpd')!r}"
            )
        if key == "filt0":
            print(
                f"         sp={obj.get('sp')!r} manSpd={obj.get('manSpd')!r} "
                f"minSpd={obj.get('minSpd')!r} maxSpd={obj.get('maxSpd')!r}"
            )


def _snap(raw: dict[str, Any], state) -> dict[str, Any]:
    return {
        "pump_on": state.pump_on,
        "pump_rpm": state.pump_rpm,
        "filt0.st": _st(_obj(raw, "filt0")),
        "pool.st": _st(_obj(raw, "pool")),
        "ecm0.st": _st(_obj(raw, "ecm0")),
        "ecm0.cmdSpd": _obj(raw, "ecm0").get("cmdSpd"),
        "desired.pool": (_obj(getattr(state, "raw", {}), "pool")),  # placeholder
    }


def _diagnosis(rest_keys: list[str], ws_raw: dict[str, Any], state) -> list[str]:
    notes: list[str] = []
    rest_has = any(k in rest_keys for k in ("filt0", "pool", "ecm0"))
    ws_has = any(isinstance(ws_raw.get(k), dict) for k in ("filt0", "pool", "ecm0"))

    if not rest_has and ws_has:
        notes.append(
            "REST main shadow has no filt0/pool/ecm0 — pump state is "
            "websocket-only. HA must keep a live WS session."
        )
    elif not ws_has:
        notes.append(
            "Websocket Authorization did not deliver filt0/pool/ecm0."
        )

    flags = {
        name: _st(_obj(ws_raw, name))
        for name in ("filt0", "pool", "ecm0")
    }
    notes.append(
        f"Wire st={flags} → parsed pump_on={state.pump_on} rpm={state.pump_rpm}"
    )

    if state.pump_on:
        notes.append(
            "Controller reports filtration ON. If HA briefly shows On then Off, "
            "an idle Auto schedule was forcing pump-off after each manual ON "
            "(fixed in v1.4.1 — schedules only auto-off pumps they auto-on'd)."
        )
    else:
        notes.append(
            "Controller reports filtration OFF right now."
        )

    notes.append(
        "HA command: filtration/setFilterPumpState "
        'payload={"state":{"desired":{"pool":{"st":N}}},"clientToken":'
        "userId|auth|appClientId}"
    )
    return notes


def _planned_frame(client: Any, want_st: int) -> dict[str, Any]:
    token = client._client_token()  # noqa: SLF001
    parts = token.split("|")
    redacted = "|".join(
        p if i == 0 else f"<redacted:{len(p)}>" for i, p in enumerate(parts)
    )
    return {
        "version": 1,
        "action": ACTION_SET_FILTER_PUMP_STATE,
        "namespace": NAMESPACE_FILTRATION,
        "service": SERVICE_STATE_CONTROLLER,
        "target": client.serial,
        "payload": {
            "state": {"desired": {"pool": {"st": want_st}}},
            "clientToken": redacted,
            "clientToken_parts": len(parts),
        },
    }


async def _send_with_debug(client: Any, want_st: int) -> None:
    """Send pump command and print frame-by-frame debug until echo or timeout."""
    frames_before = len(client._ws_debug_frames)  # noqa: SLF001
    before = {
        "filt0": _st(_obj(client._reported, "filt0")),  # noqa: SLF001
        "pool": _st(_obj(client._reported, "pool")),  # noqa: SLF001
        "ecm0": _st(_obj(client._reported, "ecm0")),  # noqa: SLF001
        "rpm": client.get_state().pump_rpm,
    }
    print("\n=== BEFORE command ===")
    print(json.dumps(before, indent=2))
    print("\n=== OUTBOUND frame ===")
    print(json.dumps(_planned_frame(client, want_st), indent=2))

    t0 = time.monotonic()
    print(f"\n>>> sending filtration/setFilterPumpState st={want_st} at t=0.0s")
    await client._send_command(  # noqa: SLF001
        namespace=NAMESPACE_FILTRATION,
        action=ACTION_SET_FILTER_PUMP_STATE,
        delta={"pool": {"st": want_st}},
        optimistic=False,
    )

    deadline = time.monotonic() + 15.0
    last_count = frames_before
    echoed = False
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        frames = client._ws_debug_frames  # noqa: SLF001
        if len(frames) > last_count:
            for frame in frames[last_count:]:
                elapsed = time.monotonic() - t0
                print(f"\n<<< WS frame +{elapsed:.2f}s: {json.dumps(frame)}")
            last_count = len(frames)

        raw = client._reported  # noqa: SLF001
        desired = client._desired  # noqa: SLF001
        for key in ("pool", "filt0", "ecm0"):
            rep = _obj(raw, key)
            des = _obj(desired, key) if isinstance(desired, dict) else {}
            if _st(rep) == want_st:
                echoed = True
                print(
                    f"\n*** ECHO OK via reported.{key}.st={want_st} "
                    f"at +{time.monotonic() - t0:.2f}s"
                )
                break
            if _st(des) == want_st:
                print(
                    f"… desired.{key}.st={want_st} at "
                    f"+{time.monotonic() - t0:.2f}s (waiting for reported)"
                )
        if echoed:
            break

    after = {
        "filt0": _st(_obj(client._reported, "filt0")),  # noqa: SLF001
        "pool": _st(_obj(client._reported, "pool")),  # noqa: SLF001
        "ecm0": _st(_obj(client._reported, "ecm0")),  # noqa: SLF001
        "rpm": client.get_state().pump_rpm,
        "desired": {
            key: _st(_obj(client._desired, key))  # noqa: SLF001
            for key in ("pool", "filt0", "ecm0")
        },
        "elapsed_s": round(time.monotonic() - t0, 2),
        "frames_delta": len(client._ws_debug_frames) - frames_before,  # noqa: SLF001
    }
    print("\n=== AFTER command ===")
    print(json.dumps(after, indent=2))
    _dump_pump_block("WS pump objects after command", client._reported)  # noqa: SLF001

    if not echoed:
        raise RuntimeError(
            f"No remote echo of st={want_st} within 15s "
            f"(frames_delta={after['frames_delta']})"
        )

    # Align local cache the same way the integration does on success.
    client._reported = _api._deep_merge(  # noqa: SLF001
        client._reported,  # noqa: SLF001
        {
            "pool": {"st": want_st},
            "filt0": {"st": want_st},
            "ecm0": {"st": want_st},
        },
    )


async def _run(args: argparse.Namespace) -> int:
    email = args.email or os.environ.get("TCX_EMAIL", "")
    password = args.password or os.environ.get("TCX_PASSWORD", "")
    serial = args.serial or os.environ.get("TCX_SERIAL", "")

    want_on = bool(args.on)
    want_off = bool(args.off)
    if want_on and want_off:
        print("Pass only one of --on / --off.", file=sys.stderr)
        return 2
    if (want_on or want_off) and not args.i_know:
        print(
            "Refusing real pump on/off without --i-know.\n"
            "  Diagnose only:  python3 scripts/tcx_pump_probe.py -v\n"
            "  Safe echo test: python3 scripts/tcx_pump_probe.py --verify-echo -v\n"
            "  Real ON:        python3 scripts/tcx_pump_probe.py --on --i-know -v",
            file=sys.stderr,
        )
        return 2

    if not args.mock and (not email or not password):
        print(
            "Missing credentials. Set TCX_EMAIL and TCX_PASSWORD "
            "(and optional TCX_SERIAL).",
            file=sys.stderr,
        )
        return 2

    if want_on:
        mode = "TURN PUMP ON (debug)"
    elif want_off:
        mode = "TURN PUMP OFF (debug)"
    elif args.verify_echo:
        mode = "VERIFY-ECHO (re-send current st — no intended change)"
    else:
        mode = "READ-ONLY diagnose"
    print("Mode:", mode)

    timeout = aiohttp.ClientTimeout(total=90)
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
        print(
            "Login OK "
            f"(app_client_id={'yes' if client._app_client_id else 'no'} "  # noqa: SLF001
            f"session_token={'yes' if client._session_client_token else 'no'})"  # noqa: SLF001
        )

        if not client.serial:
            devices = await client.async_list_tcx_devices()
            for device in devices:
                print(
                    f"  - {device.get('serial_number')} "
                    f"name={device.get('name')!r}"
                )
            if not devices:
                print("No TCX controllers found.", file=sys.stderr)
                return 1
            client.serial = str(devices[0]["serial_number"])
            print(f"Using serial {client.serial}")

        rest_keys: list[str] = []
        print("\nFetching REST shadow…")
        try:
            shadow = await client.async_get_shadow()
            reported = (shadow.get("state") or {}).get("reported") or {}
            rest_keys = sorted(reported)
            print(
                f"REST keys={len(rest_keys)} "
                f"filt0={'filt0' in reported} pool={'pool' in reported} "
                f"ecm0={'ecm0' in reported}"
            )
            print(
                f"systemMode={reported.get('systemMode')!r} "
                f"tempSetting={reported.get('tempSetting')!r} "
                f"aws={reported.get('aws')!r}"
            )
            _dump_pump_block("REST pump objects", reported)
        except Exception as err:  # noqa: BLE001
            print(f"REST shadow failed: {err}")

        print("\nConnecting websocket…")
        try:
            await client.async_connect_ws(wait_for_auth=True)
            print(
                f"WS auth_event={client._ws_auth_event.is_set()} "  # noqa: SLF001
                f"keys={sorted(client._reported)}"  # noqa: SLF001
            )
        except Exception as err:  # noqa: BLE001
            print(f"WS connect failed: {err}", file=sys.stderr)
            await client.async_close()
            return 1

        ws_raw = dict(client._reported)  # noqa: SLF001
        _dump_pump_block("WS / merged pump objects", ws_raw)
        state = client.get_state()
        print("\n=== Parsed HA view ===")
        print(
            json.dumps(
                {
                    "serial": state.serial,
                    "online": state.online,
                    "pump_on": state.pump_on,
                    "pump_rpm": state.pump_rpm,
                    "pump_min_rpm": state.pump_min_rpm,
                    "pump_max_rpm": state.pump_max_rpm,
                    "water_temp_f": state.water_temp_f,
                    "temp_unit_celsius": state.temp_unit_celsius,
                    "systemMode": ws_raw.get("systemMode"),
                },
                indent=2,
            )
        )

        sh = ws_raw.get("sh")
        if isinstance(sh, dict) and sh:
            print("\n=== TCX panel schedules (sh.*) ===")
            print(
                "These run ON THE CONTROLLER, independent of Home Assistant "
                "Auto schedule. A Pool Filtration of=21:00 will turn the pump "
                "off at 9pm local even if HA just turned it on."
            )
            for sid, slot in sorted(sh.items(), key=lambda item: str(item[0])):
                if not isinstance(slot, dict):
                    continue
                print(
                    f"  [{sid}] {slot.get('id')!r} lc={slot.get('lc')!r} "
                    f"on={slot.get('on')!r} of={slot.get('of')!r} "
                    f"en={slot.get('en')!r} days={slot.get('dw')!r}"
                )

        print("\n=== Diagnosis ===")
        for note in _diagnosis(rest_keys, ws_raw, state):
            print(f"- {note}")
        if isinstance(sh, dict):
            for slot in sh.values():
                if (
                    isinstance(slot, dict)
                    and slot.get("lc") == "pool"
                    and int(slot.get("en") or 0) == 1
                ):
                    print(
                        f"- Panel schedule {slot.get('id')!r}: "
                        f"{slot.get('on')} → {slot.get('of')} every day "
                        f"in the controller timezone. Manual ON outside that "
                        f"window may be turned back OFF by the panel."
                    )

        if not (args.verify_echo or want_on or want_off):
            print(
                "\nRESULT: diagnose-only. Next steps:\n"
                "  python3 scripts/tcx_pump_probe.py --verify-echo -v\n"
                "  python3 scripts/tcx_pump_probe.py --on --i-know -v"
            )
            await client.async_close()
            return 0 if ("filt0" in ws_raw or "ecm0" in ws_raw) else 1

        if want_on:
            want_st = 1
        elif want_off:
            want_st = 0
        else:
            want_st = 1 if state.pump_on else 0

        try:
            if args.mock:
                await client.async_set_filter_pump(bool(want_st))
            else:
                await _send_with_debug(client, want_st)
        except Exception as err:  # noqa: BLE001
            print(f"\nCommand FAILED: {err}", file=sys.stderr)
            print("\n=== Full WS frame log ===")
            print(json.dumps(client._ws_debug_frames, indent=2))  # noqa: SLF001
            await client.async_close()
            return 1

        # Watch for 8s for a surprising remote flip (schedule / second client).
        if want_on or want_off:
            print("\n=== Post-command watch (8s) for unexpected st flips ===")
            watch_until = time.monotonic() + 8.0
            last = (
                _st(_obj(client._reported, "pool")),  # noqa: SLF001
                _st(_obj(client._reported, "filt0")),  # noqa: SLF001
                _st(_obj(client._reported, "ecm0")),  # noqa: SLF001
            )
            while time.monotonic() < watch_until:
                await asyncio.sleep(0.5)
                cur = (
                    _st(_obj(client._reported, "pool")),  # noqa: SLF001
                    _st(_obj(client._reported, "filt0")),  # noqa: SLF001
                    _st(_obj(client._reported, "ecm0")),  # noqa: SLF001
                )
                if cur != last:
                    print(f"st changed {last} → {cur}")
                    last = cur
            final = client.get_state()
            print(
                f"Final: pump_on={final.pump_on} rpm={final.pump_rpm} "
                f"st(pool,filt0,ecm0)={last}"
            )
            if want_on and not final.pump_on:
                print(
                    "\nWARNING: pump ended OFF after we turned it ON. "
                    "Something else (HA Auto schedule, iAquaLink app, or "
                    "another client) issued an OFF. In HA, turn Auto schedule "
                    "OFF while testing, or update to v1.4.1+.",
                    file=sys.stderr,
                )

        if args.verify_echo and not (want_on or want_off):
            print(
                "\nRESULT: PASS — echo of current st accepted "
                "(pump state intentionally unchanged)."
            )
        else:
            print(
                "\nRESULT: PASS — cloud echoed the command. "
                "Confirm the physical pump matches if you used --on/--off."
            )
        await client.async_close()
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--serial", default="")
    parser.add_argument(
        "--verify-echo",
        action="store_true",
        help="Re-send current pump st (safe command-path test)",
    )
    parser.add_argument(
        "--on",
        action="store_true",
        help="Turn filter pump ON with frame-level debug (requires --i-know)",
    )
    parser.add_argument(
        "--off",
        action="store_true",
        help="Turn filter pump OFF with frame-level debug (requires --i-know)",
    )
    parser.add_argument(
        "--i-know",
        action="store_true",
        help="Required with --on/--off (pump may be dry / damaged if misused)",
    )
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
