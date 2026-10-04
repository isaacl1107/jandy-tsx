"""Tests for TCX shadow parsing."""

from custom_components.jandy_tcx.api import (
    _merge_reported,
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
    reported["lvh1"]["en"] = 1
    reported["water"]["value"] = 825
    state = parse_reported(reported)
    assert state.water_temp_f == 82.5
    assert state.heater_enabled is True
    assert state.heater_running is True


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
