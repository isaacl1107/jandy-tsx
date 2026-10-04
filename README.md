# Jandy AquaLink TCX for Home Assistant

Control and **auto-schedule** your Jandy AquaLink **TCX** pool heater, filter pump, and lights from Home Assistant — including on a **Raspberry Pi**.

This is a **custom integration** (not a Supervisor add-on). That matters because the popular [liptonj/hassio-addons](https://github.com/liptonj/hassio-addons) TCX client is **amd64-only** and will not install on Pi / ARM. This integration runs inside Home Assistant Core itself, so it works on:

- Home Assistant OS (Pi, x86, …)
- Home Assistant Supervised
- Home Assistant Container / Core

## What you get

| Entity | Purpose |
|--------|---------|
| `climate.*_heater` | Heater on/off + setpoint (°F) |
| `switch.*_filter_pump` | Filter pump |
| `switch.*_heater_enable` | Heater enable |
| `switch.*_auto_schedule` | Pause / resume local schedules |
| `number.*_heater_setpoint` | Setpoint slider |
| `number.*_pump_rpm` | VSP speed |
| `light.*_pool_light` | Pool light + color programs |
| `sensor.*_pool_temperature` | Water temp |
| `sensor.*_heater_schedule` | Schedule status + next on/off |
| `binary_sensor.*_heater_running` | Heater actively firing |

## Install on your Home Assistant (Raspberry Pi)

### Option A — HACS (recommended)

1. Install [HACS](https://hacs.xyz/) if you do not have it.
2. **HACS → Integrations → ⋮ → Custom repositories**
3. Add this repository URL, category **Integration**
4. Find **Jandy AquaLink TCX** → **Download**
5. **Restart Home Assistant**
6. **Settings → Devices & services → Add integration → Jandy AquaLink TCX**
7. Sign in with your iAquaLink / Jandy email + password

### Option B — Manual copy

1. Copy the folder `custom_components/jandy_tcx` into your Home Assistant config:

```text
/config/custom_components/jandy_tcx/
```

2. Restart Home Assistant  
3. **Settings → Devices & services → Add integration → Jandy AquaLink TCX**

## Auto-scheduling the heater

Schedules run **inside this integration** (no Node-RED / extra add-on).

1. Open the integration → **Configure**
2. Edit the **Schedules JSON**

Example:

```json
[
  {
    "id": "weekday-heat",
    "target": "heater",
    "days": [0, 1, 2, 3, 4],
    "start": "10:00",
    "end": "18:00",
    "enabled": true,
    "setpoint_f": 84
  },
  {
    "id": "daily-filter",
    "target": "pump",
    "days": [0, 1, 2, 3, 4, 5, 6],
    "start": "08:00",
    "end": "12:00",
    "enabled": true
  }
]
```

- `days`: Monday = `0` … Sunday = `6`
- `target`: `heater` or `pump`
- Times use your Home Assistant timezone
- When the heater schedule is active, the filter pump is forced on (safe interlock)
- Toggle **Auto schedule** off for full manual control

Services:

- `jandy_tcx.set_schedule_enabled` — pause/resume schedules
- `jandy_tcx.apply_schedules_now` — re-evaluate immediately

## Offline / mock mode

When adding the integration, enable **Use offline mock controller** to try entities and schedules without cloud credentials.

## Why not the Supervisor add-on?

The upstream TCX add-on image is published for **amd64 only**. Raspberry Pi Home Assistant is **aarch64/armv7**, so the Add-on Store rejects it. A custom integration avoids that architecture lock-in entirely.

## Development / tests

```bash
python3 -m pip install pytest
python3 -m pytest tests -q
```

## Disclaimer

Not affiliated with Zodiac, Fluidra, or Jandy. Uses the same public iAquaLink / Zodiac cloud APIs as the official app. Use at your own risk.
