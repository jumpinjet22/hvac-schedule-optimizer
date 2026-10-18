# HVAC Schedule Optimizer

A self-contained, weather-aware thermostat scheduler for Home Assistant.

It learns how your house behaves from the history Home Assistant already has, checks that
its model actually matches reality, then designs a daily schedule that uses less AC energy
while staying inside comfort limits you set. It also runs the schedule itself, so you do not
need another scheduler integration.

> Version 0.6. Cooling and heating. Single thermostat per entry. Includes a dashboard card.

## How it works

1. Reads the thermostat's recorded indoor temperature, `hvac_action` (cooling or not) and
   setpoint from the recorder, plus past and forecast weather and sunshine from
   [Open-Meteo](https://open-meteo.com) (free, no key).
2. Fits a simple model of the house:

   `dTin/dt = k*(Tout - Tin) + g*Sun + q - c*AC_on`

   (leakiness, solar gain, steady internal heat, and how fast your AC pulls the house down).
3. **Learns where your thermostat really switches.** Many thermostats (especially ones on
   remote or averaged sensors) keep cooling well below the setpoint on the reading used here.
   The start and stop points are read from every AC cycle in history, and used only if they
   replay your real runtime better than the configured hysteresis.
4. **Learns what the simple model misses**: heat the house holds after a sunny afternoon, and
   an AC losing cooling on very hot days. This correction is checked on the last two days it
   did not learn from and is only used if it predicts them better.
5. **Checks the model.** It replays the last week with your real setpoints and compares the
   AC runtime it predicts against what really happened. If they disagree by more than 30%,
   the proposal is marked "needs review" and will not be applied automatically.
6. Simulates a few thousand day/night schedules against the next 24 hours of forecast, then
   refines the best ones freely: moving, nudging, adding or dropping one change at a time (up
   to 6 changes a day, at least an hour apart). This is how it finds pre-cooling before a hot
   afternoon the AC cannot keep up with. Anything that breaks a comfort limit is discarded and
   the cheapest is proposed. A new proposal only replaces the previous one if it is clearly
   better, so it does not flip between refreshes.
7. The active schedule is run by this integration: it sets the thermostat at each node time.

It re-runs every 15 minutes so the chart keeps up with the clock. Only the daily run time can
auto-apply.

### Heating

Heating works the same way, for any common setup:

- **Gas furnace, electric furnace or baseboard, heat pump with backup strips, or dual fuel**
  (heat pump with gas below a switch-over temperature). Pick it on the **Heating** options page,
  and enter rough prices and equipment sizes on the **Costs** page so heat and cooling are
  weighed fairly.
- The house model learns how fast your heat warms the house, and where the thermostat really
  starts and stops it, from the heating already in your history. Rooms learn whether they run
  cold while the heat is on.
- It keeps the house above minimum temperatures you set for awake and sleep hours, at the
  lowest cost. A gas furnace gets deeper overnight setbacks with a warm-up before you wake.
  A heat pump gets gentler ones, because a big or cold-weather warm-up runs the backup strips.
- **heat_cool (auto) thermostats:** the lower setpoint is the heat and the upper one the cooling.
  Both are scheduled, and they are never set closer than the gap you choose (ecobee needs 5 °F
  by default, others 2 to 3 °F). The thermostat still does the switching itself, so it keeps
  working if Home Assistant is down.
- **Heat only or cool only:** whichever setpoint the mode uses is scheduled. A schedule entry
  can carry `heat:` as well as `temp:`; in cool mode the heat values are kept but not written.
- **Thermostats without heat_cool** can opt in to having the integration switch between heat
  and cool when the forecast plan needs it. It holds a mode at least 3 hours, backs off for
  12 hours after anyone picks a mode by hand, and never switches a thermostat that is off.
  If Home Assistant is down the thermostat stays in its last mode, so leave this off if that
  would be a problem in your climate.
- Comfort learning works for heat too: turning the heat **up** on 2 days for the same reason (a
  cold room, a cold day, or that time of day) raises that minimum, with the same limits and
  fading as on the cooling side.

### Learning from your changes

When you change the setpoint yourself (in the app or at the wall) while the schedule is running,
the integration keeps the change together with what the house was doing: every room and whether
it was warming, outdoor temperature, humidity, and recent AC runtime. It then works out the likely
reason for a turn-down: a room running warm, sticky air, a hot day, or just that time of day.

- It needs the same reason on **2 days within a week** (or 3 within two weeks) before it uses it.
- Learned limits only ever **tighten** the limits you set, never by more than 3 °F (1.5 °C).
- They fade if you stop making the change, and turning it back up counts against them.
- A change you undo within 10 minutes is ignored, and so are jumps over 5 °F.
- The card lists what it learned. Tap × to forget one, or use **Forget everything learned**
  (or the `hvac_schedule_optimizer.reset_learned_comfort` service). The **Learn from my
  changes** switch turns it off.

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
   - When the daily run happens (default 04:30; this is the only run that can auto-apply) and
     how many days of history to learn from (default 10, which matches the recorder default).
     The chart and proposal also refresh every 15 minutes so "now" keeps up with the clock.
   - **Room temperature sensors** (optional, as many as you like). Each room learns how it runs
     compared with the thermostat, from outdoor temperature, sun and recent AC runtime. On the
     next page you pick when each room counts against the comfort limits: always, awake hours,
     sleep hours, or never (shown on the card only). A room needs about 3 days of readings, and
     a model that tracks it closely, before it limits the schedule; until then the card marks it
     "learning".
   - **Outdoor temperature sensors** (optional, as many as you like). Their median replaces
     Open-Meteo's past weather when learning, and the usual gap between your sensors and the
     forecast at each hour of the day corrects the forecast. With three or more sensors, one
     sitting in the sun is outvoted.
   - **Fan** (only shown when your thermostat reports fan modes). Turn on fan control and pick
     which of your thermostat's own modes is *normal* (blower runs with cooling only) and which
     is *circulate* (blower runs on its own). The integration guesses both from the mode names.
     Also set the blower's power on circulate and a humidity limit above which it will not
     circulate.
   - **Heating** (only shown when your thermostat can heat): what heats the house, minimum
     temperatures for awake and sleep hours, your current heat setpoints, the smallest gap your
     thermostat allows between heat and cool setpoints, and (for thermostats without heat_cool)
     whether the integration may switch modes for you.
   - **Costs**: AC power, electricity price, and only what your heat source needs (gas price and
     furnace rating, electric heat power, or backup strip power and lockout temperature).
3. How the fan is used: every room learns how much the blower running on its own evens it out,
   once the history holds about 4 hours of fan-only running. The optimizer then tries short
   circulate windows (around bedtime, overnight, late afternoon, all day) only for schedules that
   fail because of a room, and keeps one only if it costs less than cooling the whole house
   harder. A schedule node with `fan: true` circulates until the next node. The fan always goes
   back to the normal mode when a window ends, when the schedule is paused, and when the
   integration unloads, and it is never touched while the schedule has no fan nodes.
4. **Turn off any other scheduler for this thermostat** (Climate Scheduler, automations that
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
| `switch.*_learn_from_my_changes` | On: manual setpoint changes are kept and repeated turn-downs tighten the comfort limits. |
| `button.*_optimize_now` | Run the optimizer immediately. |
| `button.*_apply_proposed_schedule` | Make the proposal the active schedule (refused if the model is not trusted). |
| `button.*_revert_schedule` | Go back to the previous schedule. |

Service `hvac_schedule_optimizer.set_schedule` sets the active schedule by hand:

```yaml
service: hvac_schedule_optimizer.set_schedule
data:
  nodes:
    - { time: "07:00", temp: 74, heat: 68 }   # heat is optional
    - { time: "22:00", temp: 70, heat: 65 }
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
comfort limit, and when the AC runs (and would run). On the past side, faint dashed lines show
what the house and each room were predicted to do by the planning run that day, next to what
really happened, and the legend shows each one's average miss (for example `±0.4°`). Small
markers show your manual setpoint changes. Hover or touch the chart for exact values,
including how far each reading was from its prediction.
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
- Turn off the thermostat's own built-in schedule too: its changes would look like yours to the
  comfort learning.
- Planning forecasts (14 days) and your manual changes (60 days) are stored in `.storage`.
- It acts in `cool`, `heat` and `heat_cool` (or `auto` with two setpoints). In `off`, `dry` and
  `fan_only` it leaves the thermostat alone.
- Setpoints are clamped to the thermostat's own min and max.
- Schedules are stored in `.storage` and survive restarts. The previous schedule is always kept.

## Limitations (planned work)

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
