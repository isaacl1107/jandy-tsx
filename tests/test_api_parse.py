"""Tests for TCX shadow parsing."""

from custom_components.jandy_tcx.api import mock_reported, parse_reported


def test_parse_mock_reported():
    state = parse_reported(mock_reported("ABC123"), serial="ABC123")
    assert state.serial == "ABC123"
    assert state.water_temp_f == 78.0
    assert state.heater_setpoint_f == 84.0
    assert state.heater_enabled is False
    assert state.pump_on is True
    assert state.pump_rpm == 2400
    assert state.swc_percent == 50


def test_parse_tenths_and_heater_running():
    reported = mock_reported()
    reported["TspBdy0"]["heatEnabled"] = True
    reported["lvh1"]["en"] = 1
    reported["water"]["value"] = 825
    state = parse_reported(reported)
    assert state.water_temp_f == 82.5
    assert state.heater_enabled is True
    assert state.heater_running is True
