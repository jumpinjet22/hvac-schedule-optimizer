# HVAC Schedule Optimizer

A self-contained, weather-aware thermostat scheduler for Home Assistant.

It learns how your house behaves from the history Home Assistant already has, checks that
its model actually matches reality, then designs a daily schedule that uses less AC energy
while staying inside comfort limits you set. It also runs the schedule itself, so you do not
need another scheduler integration.

> Version 0.2. Cooling only. Single thermostat per entry. Includes a dashboard card.

## How it works

1. Reads the thermostat's recorded indoor temperature, `hvac_action` (cooling or not) and
   setpoint from the recorder, plus past and forecast weather and sunshine from
   [Open-Meteo](https://open-meteo.com) (free, no key).
2. Fits a simple model of the house:

   `dTin/dt = k*(Tout - Tin) + g*Sun + q - c*AC_on`

   (leakiness, solar gain, steady internal heat, and how fast your AC pulls the house down).
3. **Checks the model.** It replays the last week with your real setpoints and compares the
   AC runtime it predicts against what really happened. If they disagree by more than 30%,
   the proposal is marked "needs review" and will not be applied automatically.
4. Simulates a few thousand candidate daily schedules (day and night setpoints, wake and
   sleep times, and an optional afternoon coast) against the next 24 hours of forecast,
   discards any that break your comfort limits, and picks the cheapest.
5. The active schedule is run by this integration: it sets the thermostat at each node time.

The savings number is a relative compressor-energy estimate, not kWh. Judge the real effect
by watching AC runtime over a few days.

## Install

### HACS (custom repository)

1. HACS, then the three dots, then **Custom repositories**.
2. Add `https://github.com/jumpinjet22/hvac-schedule-optimizer` with category **Integration**.
3. Install **HVAC Schedule Optimizer** and restart Home Assistant.
4. **Settings, Devices & services, Add integration, HVAC Schedule Optimizer.**

### Manual

Copy `custom_components/hvac_schedule_optimizer` into your Home Assistant `config/custom_components/`
folder and restart.

## Setup

1. Pick your thermostat (and optionally a separate indoor temperature sensor).
2. Open **Configure** on the integration and set:
   - Comfort limits: maximum temperature while awake and while asleep, and the hours.
   - **Your current schedule** (day and night temperatures and times). This is the baseline
     for the savings comparison and the starting schedule.
   - When the optimizer runs each day (default 04:30) and how many days of history to learn
     from (default 10, which matches the recorder default).
3. **Turn off any other scheduler for this thermostat** (Climate Scheduler, automations that
   set the temperature on a timer) so they do not fight.

## Entities

| Entity | What it does |
| --- | --- |
| `sensor.*_status` | `ready`, `needs_review`, `no_comfortable_schedule`, `error`. Attributes hold the fitted model, validation and any error message. |
| `sensor.*_proposed_schedule` | What the optimizer wants to run, with expected indoor range and AC hours. |
| `sensor.*_active_schedule` | What is running now, where it came from, the previous schedule, and the next change. |
| `sensor.*_estimated_energy_change` | Estimated change against your baseline, in percent. |
| `sensor.*_model_match` | Predicted over actual AC runtime. Near 1.0 is good. |
| `switch.*_run_schedule` | On: this integration sets the thermostat at each node time. |
| `switch.*_auto_apply_optimizer` | On: the daily run replaces the active schedule when the model is trusted. |
| `button.*_optimize_now` | Run the optimizer immediately. |
| `button.*_apply_proposed_schedule` | Make the proposal the active schedule (refused if the model is not trusted). |
| `button.*_revert_schedule` | Go back to the previous schedule. |

Service `hvac_schedule_optimizer.set_schedule` sets the active schedule by hand:

```yaml
service: hvac_schedule_optimizer.set_schedule
data:
  nodes:
    - { time: "07:00", temp: 74 }
    - { time: "22:00", temp: 70 }
```

## Dashboard card

The integration ships its own Lovelace card and loads it automatically, so there is no resource
to add. Edit a dashboard, **Add card**, pick **HVAC Schedule**, or use YAML:

```yaml
type: custom:hvac-schedule-card
climate_entity: climate.living_room_thermostat  # optional if you only have one entry
title: Cooling schedule                          # optional
```

It shows 24 hours of history and the next 24 hours on one chart: outdoor temperature behind
everything, your indoor temperature, the active schedule, the proposed schedule (dashed), your
comfort limit, and when the AC runs (and would run). Hover or touch the chart for exact values.
Under the chart is a runtime panel: AC hours over the last 24h, predicted hours for the next 24h under your active and proposed schedules, and minutes of runtime per hour for each. Below that are the active and proposed schedule times, **Apply proposed**, **Optimize now**,
**Revert**, and the Run and Auto apply switches. Buttons and switches need an admin user.

If the card does not show up after installing, restart Home Assistant and hard refresh the browser.

## Suggested first week

1. Leave **Run schedule** and **Auto apply** off. Press **Optimize now** and read the status
   sensor. Check `model_match` is roughly 0.8 to 1.2.
2. Compare the proposed schedule with yours. If it looks sane, press **Apply proposed schedule**,
   then turn **Run schedule** on.
3. Watch AC runtime for a few days. When you trust it, turn **Auto apply** on.

## Behavior notes

- The scheduler writes the setpoint only at node times (and once at startup or when enabled),
  so a manual change at the thermostat holds until the next node.
- It only acts while the thermostat is in `cool` mode.
- Setpoints are clamped to the thermostat's own min and max.
- Schedules are stored in `.storage` and survive restarts. The previous schedule is always kept.

## Limitations (planned work)

- Heating season is not modeled yet.
- One schedule for every day of the week.
- Needs a few days of AC cycling in the recorder to fit the model.
- Energy savings assume compressor power rises about 1.2% per degree of outdoor temperature
  above ~82 F. It is a rough constant, not measured from your unit.

## Development

The model and optimizer live in `custom_components/hvac_schedule_optimizer/engine.py` with no
Home Assistant imports, so they run anywhere:

```bash
pip install pytest
python -m pytest -q tests
```

## License

MIT
