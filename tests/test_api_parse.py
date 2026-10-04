"""Tests for TCX shadow parsing."""

import asyncio

import aiohttp
import pytest

from custom_components.jandy_tcx.api import (
    TcxClient,
    _extract_desired_reported,
    _merge_reported,
    _summarize_aux,
    mock_reported,
    parse_reported,
)


def test_parse_mock_reported():
    state = parse_reported(mock_reported("ABC123"), serial="ABC123")
    assert state.serial == "ABC123"
    assert state.water_temp_f == 78.0
    assert state.heater_setpoint_f == 84.0
    assert state.heater_enabled is False
    assert state.pump_on is True
    assert state.pump_rpm == 2400
    assert state.swc_percent == 50
    assert state.water_feature_available is True
    assert state.water_feature_key == "aux0"
    assert state.water_feature_on is False
    assert state.light_key == "aux1"
    assert state.light_is_color is True
    assert state.light_available is True


def test_parse_tenths_and_heater_running():
    reported = mock_reported()
    reported["TspBdy0"]["heatEnabled"] = True
    reported["lvh1"]["en"] = 6  # heating code
    reported["water"]["value"] = 825
    state = parse_reported(reported)
    assert state.water_temp_f == 82.5
    assert state.heater_enabled is True
    assert state.heater_running is True
    assert state.temp_unit_celsius is False


def test_parse_celsius_temp_setting_converts_to_f():
    """tempSetting=0 means wire tenths are °C — HA always exposes °F."""
    from custom_components.jandy_tcx.api import _wire_temp_to_f, _f_to_wire_temp

    assert _wire_temp_to_f(283, celsius=True) == 82.9  # 28.3°C
    assert _wire_temp_to_f(256, celsius=True) == 78.1  # 25.6°C
    assert _f_to_wire_temp(89.1, celsius=True) == 317  # 31.7°C

    reported = mock_reported()
    reported["tempSetting"] = 0
    reported["water"]["value"] = 283
    reported["air"] = {"value": 256, "us": 1}
    reported["TspBdy0"]["waterTempSet"] = 317
    del reported["airTemp"]
    state = parse_reported(reported)
    assert state.temp_unit_celsius is True
    assert state.water_temp_f == 82.9
    assert state.air_temp_f == 78.1
    assert state.heater_setpoint_f == 89.1


def test_parse_pump_and_swc_from_live_shape():
    reported = mock_reported()
    reported["filt0"]["st"] = 1
    reported["pool"]["st"] = 1
    reported["ecm0"]["st"] = 1
    reported["ecm0"]["cmdSpd"] = 2500
    reported["swc0"] = {"outputPcnt": 0, "stdPoolPcnt": 40, "salinity": 30}
    state = parse_reported(reported)
    assert state.pump_on is True
    assert state.pump_rpm == 2500
    assert state.swc_percent == 0


def test_light_fallback_when_labels_missing():
    reported = mock_reported()
    # Strip identifying light labels but keep aux1 present.
    reported["aux1"] = {"st": 0, "en": 1, "fr": "Relay 1"}
    del reported["aux0"]  # remove WF so it isn't confused
    state = parse_reported(reported)
    assert state.light_key == "aux1"
    assert state.light_available is True


def test_aux_pump_name_counts_as_water_feature():
    reported = mock_reported()
    reported["aux0"] = {
        "st": 1,
        "en": 1,
        "app": "AUX",
        "fr": "Aux Pump",
        "ty": 1,
    }
    state = parse_reported(reported)
    assert state.water_feature_available is True
    assert state.water_feature_key == "aux0"
    assert state.water_feature_on is True
    assert state.water_feature_name == "Aux Pump"
    assert "aux0" in state.aux_circuits


def test_generic_aux_exposed_and_light_only_panel():
    reported = mock_reported()
    # Live-style panel: light on aux0, no WF label — plus a spare aux2.
    reported["aux0"] = {
        "st": 0,
        "en": 1,
        "app": "POOL_LT",
        "et": "JL",
        "fr": "Pool Light",
        "ty": 6,
        "currClr": 4,
    }
    del reported["aux1"]
    reported["aux2"] = {"st": 0, "en": 1, "fr": "Blower", "ty": 1}
    state = parse_reported(reported)
    assert state.light_key == "aux0"
    assert state.water_feature_available is False
    assert "aux2" in state.aux_circuits
    assert state.aux_circuits["aux2"]["name"] == "Blower"
    assert state.aux_circuits["aux2"]["kind"] == "aux"


def test_merge_namespace_keyed_authorization_payload():
    """WS Authorization full-state is namespace-keyed (main/pib0/zig/…)."""
    payload = {
        "main": {
            "state": {
                "reported": {
                    "sn": "RJEB01",
                    "aws": {"status": "connected"},
                    "name": "Backyard",
                }
            }
        },
        "pib0": {
            "state": {
                "reported": {
                    "water": {"value": 820, "us": 1},
                    "aux1": {
                        "st": 0,
                        "app": "POOL_LT",
                        "et": "JL",
                        "fr": "Pool Light",
                        "currClr": 2,
                    },
                }
            }
        },
        "zig": {
            "state": {
                "reported": {
                    "zig": {"status": "ok"},
                    "auxz0": {"st": 0, "fr": "Patio Light", "et": "JL"},
                }
            }
        },
        "data": [{"ignored": True}],
    }
    merged = _merge_reported(payload)
    assert merged["sn"] == "RJEB01"
    assert merged["aux1"]["app"] == "POOL_LT"
    assert merged["auxz0"]["fr"] == "Patio Light"
    state = parse_reported(merged, serial="RJEB01")
    assert state.light_key == "aux1"
    assert state.light_available is True
    assert state.water_temp_f == 82.0


def test_extract_desired_reported_and_aux_summary():
    desired, reported = _extract_desired_reported(
        {
            "state": {
                "desired": {"aux0": {"st": 1}},
                "reported": {"aux0": {"st": 0, "app": "POOL_LT"}},
            }
        }
    )
    assert desired["aux0"]["st"] == 1
    assert reported["aux0"]["st"] == 0
    assert _summarize_aux(desired) == {
        "aux0": {"st": 1, "cmdClr": None, "currClr": None}
    }


@pytest.mark.asyncio
async def test_login_reads_cognito_pool_app_client_id(monkeypatch):
    """Real Zodiac login nests appClientId under cognitoPool, not oauth."""

    class FakeResp:
        status = 200

        async def json(self, content_type=None):
            return {
                "id": 42,
                "authentication_token": "AUTHTOKEN20CHARS!!",
                "userPoolOAuth": {
                    "IdToken": "id-token",
                    "RefreshToken": "refresh",
                    "ExpiresIn": 3600,
                },
                "cognitoPool": {"appClientId": "app-client-from-cognito"},
            }

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResp()

    client = TcxClient(FakeSession(), "user@example.com", "secret")  # type: ignore[arg-type]
    await client.async_login()
    assert client._app_client_id == "app-client-from-cognito"
    assert client._session_client_token == (
        "42|AUTHTOKEN20CHARS!!|app-client-from-cognito"
    )
    assert client._client_token() == client._session_client_token


@pytest.mark.asyncio
async def test_streamer_desired_does_not_count_as_reported_success():
    """Desired-only echo must not flip local reported.st (false PASS)."""
    session = aiohttp.ClientSession()
    try:
        client = TcxClient(session, "a@b.c", "x", serial="RJEB01", mock=False)
        client._reported = {
            "aux0": {"st": 0, "app": "POOL_LT", "et": "JL", "fr": "Pool Light"}
        }
        client._ws_auth_event.set()
        # Simulate a StateStreamer desired ack with empty reported.
        await client._handle_ws_frame(
            {
                "service": "StateStreamer",
                "payload": {
                    "state": {
                        "desired": {"aux0": {"st": 1}},
                        "reported": {},
                    },
                    "metadata": {"desired": {}, "reported": {}},
                    "version": 1,
                    "timestamp": 1,
                },
            }
        )
        assert client._desired["aux0"]["st"] == 1
        assert int((client._reported.get("aux0") or {}).get("st") or 0) == 0
        ok = await client._async_wait_aux_remote(
            "aux0", 1, frames_before=0, timeout=0.5
        )
        assert ok is False
    finally:
        await session.close()
