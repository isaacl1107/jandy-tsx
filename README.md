# Jandy AquaLink TCX for Home Assistant

Control and **auto-schedule** your Jandy AquaLink **TCX** pool heater, filter pump, lights, and water feature from Home Assistant — including on a **Raspberry Pi**.

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
| `switch.*_water_feature` | Water feature / waterfall (when TCX reports one) |
| `switch.*_auto_schedule` | Pause / resume local schedules |
| `number.*_heater_setpoint` | Setpoint slider |
| `number.*_pump_rpm` | VSP speed |
| `light.*_pool_light` | Pool light + color programs |
| `sensor.*_pool_temperature` | Water temp |
| `sensor.*_heater_schedule` | Schedule status + next on/off |
| `binary_sensor.*_heater_running` | Heater actively firing |

## Install on your Home Assistant (Raspberry Pi)

Current version: **1.3.9**

### Option A — HACS (recommended)

1. Install [HACS](https://hacs.xyz/) if you do not have it.
2. **HACS → Integrations → ⋮ → Custom repositories**
3. Add `https://github.com/isaacl1107/jandy-tsx`, category **Integration**
4. Find **Jandy AquaLink TCX** → **Download** (or **Redownload** → pick latest)
5. **Restart Home Assistant**
6. **Settings → Devices & services → Add integration → Jandy AquaLink TCX**
7. Sign in with your iAquaLink / Jandy email + password

To confirm the update took effect, check `custom_components/jandy_tcx/manifest.json` shows `"version": "1.3.9"`. After restart, open the **Light** entity — attributes should include `aux_key` (for example `aux0`).

**v1.3.9** fixes light/aux writes: Zodiac login nests `appClientId` under `cognitoPool`, and without that field websocket commands were sent with a bogus `clientToken` (cloud ignored them).

### Local light probe (outside Home Assistant)

```bash
export TCX_EMAIL='your-iaqualink-email'
export TCX_PASSWORD='your-iaqualink-password'
export TCX_SERIAL='RJEB01050120260091'   # optional
python3 scripts/tcx_light_probe.py -v
```

A real PASS requires discovering an aux circuit with labels (`app`/`et`/`fr`). A bare `aux1` with only `st` is an optimistic local stub and does **not** mean the physical light changed.

### Option B — Manual copy

1. Copy the folder `custom_components/jandy_tcx` into your Home Assistant config:

```text
/config/custom_components/jandy_tcx/
```

2. Restart Home Assistant  
3. **Settings → Devices & services → Add integration → Jandy AquaLink TCX**

## Auto-scheduling the heater

Schedules run **inside this integration** (no Node-RED / extra add-on).

New installs start with **no schedules**. Add only what you want:

1. Open the integration → **Configure**
2. Choose **Manage schedules**
3. **Add** / **Edit** / **Delete** weekly windows in the UI:
   - Equipment: **heater**, **filter pump**, **pool light**, or **water feature**
   - Days of week
   - Start / end time
   - Heater setpoint (°F) when equipment is heater
   - Enabled toggle
4. Choose **Save and finish**

Notes:

- Times use your Home Assistant timezone
- Overnight windows work (for example 22:00 → 06:00)
- When a heater or water-feature schedule is active, the filter pump is forced on (safe interlock)
- Toggle **Auto schedule** off for full manual control
- If you previously installed with sample schedules, delete them in **Manage schedules**

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
