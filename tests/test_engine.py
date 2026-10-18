"""Tests for the pure Python engine. No Home Assistant needed."""

import importlib.util
import math
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "hso_engine", ROOT / "custom_components" / "hvac_schedule_optimizer" / "engine.py"
)
engine = importlib.util.module_from_spec(_spec)
sys.modules["hso_engine"] = engine
_spec.loader.exec_module(engine)

STEP = engine.STEP_S
TZ_OFFSET = -5 * 3600  # fixed local offset for the synthetic house


def _local_min(ts):
    return int(((ts + TZ_OFFSET) % 86400) // 60)


def _weather(ts):
    hod = ((ts + TZ_OFFSET) % 86400) / 3600.0
    day = ts / 86400.0
    temp = 79 + 8 * math.sin(2 * math.pi * (hod - 9) / 24) + 3 * math.sin(day / 2.3)
    sun = max(0.0, 900 * math.sin(math.pi * (hod - 6) / 12)) * (0.8 + 0.2 * math.sin(day))
    return temp, sun


def _synthetic(history_days=14, noise=0.05, seed=7, band=None, hot=0.0, weak_above=None, weak=0.0):
    """band: the true thermostat switching band; hot: extra outdoor degrees;
    weak_above/weak: the AC loses `weak` of its cooling above that outdoor temperature."""
    rng = random.Random(seed)
    s = engine.Settings.defaults(True)
    n_hist = history_days * 96
    n_hor = 96
    t0 = 1_792_000_000 - (1_792_000_000 % STEP)
    g0 = t0 - n_hist * STEP
    ts = [g0 + i * STEP for i in range(n_hist + n_hor)]

    def wxf(t):
        temp, sun = _weather(t)
        return temp + hot, sun

    wx = [wxf(t) for t in ts]
    truth = {"k": 0.09, "g": 1.1, "q": 0.15, "c": 3.2}

    mins = [m for m, _ in s.baseline]
    temps = [t for _, t in s.baseline]
    from bisect import bisect_right

    sp_hist = [temps[bisect_right(mins, _local_min(t)) - 1] for t in ts[:n_hist]]
    corr = None
    if weak_above is not None:
        corr = ([0.0] * n_hist, [truth["c"] * weak if w[0] > weak_above else 0.0 for w in wx[:n_hist]])
    Ti_true, on_true = engine.simulate(
        truth, [w[0] for w in wx[:n_hist]], [w[1] for w in wx[:n_hist]], 73.0, sp_hist,
        band if band is not None else s.hysteresis, 0, corr,
    )
    indoor = [(ts[i], Ti_true[i] + rng.gauss(0, noise)) for i in range(n_hist)]
    on = [(ts[i], float(on_true[i])) for i in range(n_hist)]
    setp = [(ts[i], float(sp_hist[i])) for i in range(n_hist)]

    # hourly weather like Open-Meteo gives
    first_hour = g0 - (g0 % 3600) - 3600
    wts = [first_hour + h * 3600 for h in range(int((ts[-1] - first_hour) / 3600) + 3)]
    inp = engine.Inputs(
        g0=g0,
        n_hist=n_hist,
        n_hor=n_hor,
        data_end=t0,
        indoor=indoor,
        on=on,
        setpoint=setp,
        weather_ts=wts,
        weather_temp=[wxf(t)[0] for t in wts],
        weather_sun=[wxf(t)[1] for t in wts],
        local_minutes=[_local_min(t) for t in ts[n_hist:]],
        settings=s,
    )
    return inp, truth


def test_resample_time_weighted():
    # value 10 for the first 300s of the bin, 20 for the other 600s -> 16.67
    pts = [(0, 10.0), (300, 20.0)]
    out = engine.resample(pts, 0, 2, 1800)
    assert abs(out[0] - (10 * 300 + 20 * 600) / 900) < 1e-9
    assert abs(out[1] - 20.0) < 1e-9


def test_resample_gap_is_none():
    pts = [(0, 10.0), (100, None), (800, 12.0)]
    out = engine.resample(pts, 0, 2, 1800)
    assert out[0] is None  # only 100s + 100s covered
    assert out[1] == 12.0


def test_solve_linear():
    x = engine._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])
    assert abs(x[0] - 1.0) < 1e-9 and abs(x[1] - 3.0) < 1e-9


def test_overnight_wrap_uses_last_node():
    s = engine.Settings.defaults(True)
    ctx = engine.make_ctx([80.0] * 4, [0.0] * 4, [60, 75, 90, 105], s)  # 01:00 to 01:45
    m = {"k": 0.09, "g": 1.0, "q": 0.1, "c": 3.0}
    r = engine.evaluate(m, [(8 * 60, 74.0), (22 * 60, 70.0)], ctx, 72.0, 0, s)
    # At 01:00 the active setpoint must be the 22:00 night value (70), so the AC runs
    assert r["hours"] > 0


def test_setpoint_at_wraps_overnight():
    nodes = [{"time": "22:00", "temp": 70}, {"time": "08:00", "temp": 74}]
    assert engine.setpoint_at(nodes, 60) == 70.0  # 01:00 -> night value
    assert engine.setpoint_at(nodes, 8 * 60) == 74.0  # exactly at the node
    assert engine.setpoint_at(nodes, 21 * 60 + 59) == 74.0
    assert engine.setpoint_at(nodes, 22 * 60) == 70.0


def test_next_change():
    nodes = [{"time": "08:00", "temp": 74}, {"time": "22:00", "temp": 70}]
    assert engine.next_change(nodes, 7 * 60) == (60, 74.0)
    assert engine.next_change(nodes, 8 * 60) == (14 * 60, 70.0)  # strictly after now
    assert engine.next_change(nodes, 23 * 60) == (9 * 60, 74.0)  # wraps to tomorrow
    single = [{"time": "06:30", "temp": 72}]
    assert engine.next_change(single, 6 * 60 + 30) == (1440, 72.0)


def test_candidates_respect_ordering():
    s = engine.Settings.defaults(True)
    count = 0
    for nodes in engine.candidates(s):
        count += 1
        mins = [n[0] for n in nodes]
        assert mins == sorted(mins)
    assert count > 1000


def test_celsius_defaults_work():
    s = engine.Settings.defaults(False)
    temps = {n[1] for nodes in engine.candidates(s) for n in nodes}
    assert all(abs(t * 2 - round(t * 2)) < 1e-6 for t in temps)  # 0.5 resolution


def test_end_to_end_recovers_house_and_optimizes():
    inp, truth = _synthetic()
    res = engine.run(inp)
    for key in ("k", "g", "q", "c"):
        err = abs(res["model"][key] - truth[key]) / abs(truth[key])
        assert err < 0.35, f"{key} off by {err:.0%}"
    assert res["validation"]["ratio"] is not None
    assert 0.7 < res["validation"]["ratio"] < 1.3
    assert res["validated"] and res["ok_to_apply"]
    best, base = res["best"], res["baseline"]
    assert best is not None and best["overshoot"] == 0
    assert best["energy_index"] <= base["energy_index"] + 1e-9
    assert res["status"] == "ready"


def test_too_little_history_raises():
    inp, _ = _synthetic(history_days=2)
    try:
        engine.run(inp)
    except engine.ModelError:
        return
    raise AssertionError("expected ModelError with only 2 days of history")


def test_no_ac_cycling_raises():
    inp, _ = _synthetic()
    inp.on = [(t, 0.0) for t, _ in inp.on]  # AC never ran
    try:
        engine.run(inp)
    except engine.ModelError:
        return
    raise AssertionError("expected ModelError when the AC never ran")


def test_series_for_card():
    inp, _ = _synthetic()
    inp.local_minutes_past = [_local_min(inp.data_end + (i - 96) * STEP) for i in range(96)]
    inp.active_nodes = [{"time": "07:00", "temp": 75}, {"time": "22:00", "temp": 71}]
    res = engine.run(inp)
    s = res["series"]
    n = 96 + 96
    assert s["now_index"] == 96
    for key in ("local_minutes", "outdoor", "indoor_actual", "ac_actual"):
        assert len(s[key]) == n, key
    for group in ("indoor", "ac"):
        for name in ("baseline", "active", "proposed"):
            assert len(s[group][name]) == n, (group, name)
            assert all(v is None for v in s[group][name][:96])
            assert all(v is not None for v in s[group][name][96:])
    assert all(v is None for v in s["indoor_actual"][96:])
    assert res["active"] is not None and res["change_vs_active_pct"] is not None
    assert res["unit"] == "°F" and res["comfort"]["awake_max"] == inp.settings.awake_max


def test_series_without_active_or_past_minutes():
    inp, _ = _synthetic()
    res = engine.run(inp)
    assert "active" not in res["series"]["indoor"]
    assert len(res["series"]["local_minutes"]) == 192


def test_runtime_prediction():
    inp, _ = _synthetic()
    res = engine.run(inp)
    rt = res["runtime"]
    assert rt["actual_24h_hours"] is not None
    p = rt["proposed"]
    assert abs(p["hours"] - res["best"]["ac_hours"]) < 0.02
    assert len(p["minutes_per_hour"]) == 24 and sum(p["minutes_per_hour"]) / 60 - p["hours"] < 0.02
    assert p["cycles"] >= 1 and 0 < p["longest_min"] <= 24 * 60


def _room_points(inp, truth_offset, sun_coef, ac_coef, noise=0.05, seed=3, days=None):
    """A room that tracks the thermostat with a known offset, sun gain and AC pull."""
    rng = random.Random(seed)
    Ti = engine.resample(inp.indoor, inp.g0, inp.n_hist, inp.data_end)
    on = engine.resample(inp.on, inp.g0, inp.n_hist, inp.data_end)
    first = inp.n_hist - days * 96 if days else 0
    pts = []
    for i in range(first, inp.n_hist):
        t = inp.g0 + i * STEP
        duty = engine._duty(on, i) if i > 0 else on[0]
        v = Ti[i] + truth_offset + sun_coef * _weather(t)[1] / 1000.0 + ac_coef * duty + rng.gauss(0, noise)
        pts.append((t, v))
    return pts


def test_room_model_learns_offset():
    inp, _ = _synthetic()
    inp.rooms = [{"id": "sensor.bedroom", "name": "Bedroom", "when": "never", "points": _room_points(inp, 2.0, 1.5, -1.0)}]
    res = engine.run(inp)
    room = res["rooms"][0]
    assert room["trusted"] and not room["limits_search"]
    assert abs(room["coef"]["offset"] - 2.0) < 0.4
    assert abs(room["coef"]["sun"] - 1.5) < 0.5
    assert abs(room["coef"]["ac"] + 1.0) < 0.4
    sr = res["series"]["rooms"][0]
    assert len(sr["actual"]) == 192 and len(sr["predicted"]["proposed"]) == 192


def test_warm_bedroom_tightens_night_schedule():
    inp, _ = _synthetic()
    plain = engine.run(inp)
    inp.rooms = [{"id": "sensor.bedroom", "name": "Bedroom", "when": "asleep", "points": _room_points(inp, 1.5, 0.0, 0.0)}]
    res = engine.run(inp)
    room = res["rooms"][0]
    assert room["limits_search"]
    best = res["best"] or res["closest"]
    night_plain = min(n["temp"] for n in plain["best"]["nodes"])
    night_room = min(n["temp"] for n in best["nodes"])
    assert night_room < night_plain  # has to run colder so the bedroom stays under the sleep limit
    if res["best"]:
        s = res["series"]
        lm = s["local_minutes"]
        pred = s["rooms"][0]["predicted"]["proposed"]
        for i in range(s["now_index"], len(lm) - 1):
            h = lm[i] // 60
            if h >= inp.settings.sleep_start or h < inp.settings.sleep_end:
                assert pred[i + 1] <= inp.settings.sleep_max + 0.06  # limits apply at the end of each bin


def test_new_room_is_shown_but_does_not_limit():
    inp, _ = _synthetic()
    inp.rooms = [{"id": "sensor.office", "name": "Office", "when": "always", "points": _room_points(inp, 5.0, 0, 0, days=1)}]
    res = engine.run(inp)
    room = res["rooms"][0]
    assert not room["trusted"] and not room["limits_search"]
    assert room["reason"].startswith("Learning")
    assert res["status"] == "ready"


def test_outdoor_sensors_median_and_forecast_bias():
    inp, _ = _synthetic()
    ts = [inp.g0 + i * STEP for i in range(inp.n_hist)]
    good = [(t, _weather(t)[0] + 3.0) for t in ts]
    good2 = [(t, _weather(t)[0] + 3.2) for t in ts]
    sunny = [(t, _weather(t)[0] + 15.0) for t in ts]  # sensor in direct sun, the median ignores it
    inp.outdoor = [good, good2, sunny]
    res = engine.run(inp)
    info = res["outdoor_sensors"]
    assert info["sensors"] == 3 and info["history_coverage"] > 0.95
    assert all(abs(b - 3.2) < 0.3 for b in info["forecast_bias_by_hour"])
    s = res["series"]
    now = s["now_index"]
    fc_time = s["start"] + now * STEP + 6 * 3600
    i = now + 24
    assert abs(s["outdoor"][i] - (_weather(fc_time)[0] + 3.2)) < 0.5


def test_no_extra_sensors_matches_previous_behavior():
    inp, _ = _synthetic()
    res = engine.run(inp)
    assert res["rooms"] == [] and res["outdoor_sensors"] is None
    assert res["series"]["rooms"] == []


def _fan_house(fan_effect=-2.0, offset=2.5):
    """History where the blower circulated 23:00-03:00 every other night, and a bedroom the fan evens out."""
    inp, _ = _synthetic()
    on = engine.resample(inp.on, inp.g0, inp.n_hist, inp.data_end)
    fan = []
    for i in range(inp.n_hist):
        t = inp.g0 + i * STEP
        h = _local_min(t) // 60
        night = int((t + TZ_OFFSET - 3 * 3600) // 86400)
        fan.append(1.0 if (h >= 23 or h < 3) and night % 2 == 0 else 0.0)
    fo = engine.fan_only(fan, on)
    Ti = engine.resample(inp.indoor, inp.g0, inp.n_hist, inp.data_end)
    rng = random.Random(5)
    pts = []
    for i in range(inp.n_hist):
        d = engine._duty(fo, i) if i > 0 else fo[0]
        pts.append((inp.g0 + i * STEP, Ti[i] + offset + fan_effect * d + rng.gauss(0, 0.05)))
    inp.fan = [(inp.g0 + i * STEP, fan[i]) for i in range(inp.n_hist)]
    inp.rooms = [{"id": "sensor.bedroom", "name": "Bedroom", "when": "asleep", "points": pts}]
    return inp


def test_merge_plan_and_fan_at():
    nodes = [(7 * 60, 75.0), (22 * 60, 72.0)]
    fan = [(6 * 60, False), (23 * 60, True)]
    merged = engine.merge_plan(nodes, fan)
    assert merged == [(7 * 60, 75.0, False), (22 * 60, 72.0, False), (23 * 60, 72.0, True), (6 * 60, 72.0, False)][:0] or merged == sorted(merged)
    assert (23 * 60, 72.0, True) in merged and (6 * 60, 72.0, False) in merged
    packed = [{"time": engine.hhmm(m), "temp": t, **({"fan": True} if f else {})} for m, t, f in merged]
    assert engine.fan_at(packed, 2 * 60) and not engine.fan_at(packed, 12 * 60)
    assert engine.setpoint_at(packed, 2 * 60) == 72.0 and engine.setpoint_at(packed, 12 * 60) == 75.0
    assert engine.merge_plan(nodes, None) == [(m, t, False) for m, t in nodes]


def test_room_learns_fan_effect():
    inp = _fan_house()
    res = engine.run(inp)
    room = res["rooms"][0]
    assert room["fan_learned"] and room["trusted"]
    assert abs(room["coef"]["fan"] + 2.0) < 0.5
    assert res["fan"]["history_fan_only_hours"] > 4


def test_fan_lets_night_run_warmer():
    inp = _fan_house()
    no_fan = engine.run(inp)
    inp.settings.fan_enabled = True
    inp.settings.fan_kw = 0.1  # variable speed blower on circulate
    res = engine.run(inp)
    assert res["fan"]["searched"] and res["fan"]["learned_rooms"] == 1
    best = res["best"]
    assert best is not None and any(n.get("fan") for n in best["nodes"])
    assert best["fan_hours"] > 0
    assert best["energy_index"] < no_fan["best"]["energy_index"]  # fan is cheaper than cooling the whole house harder
    s = res["series"]
    assert len(s["fan"]["proposed"]) == 192 and sum(s["fan"]["proposed"][96:]) > 0


def test_fan_not_used_when_blocked_or_unlearned():
    inp = _fan_house()
    inp.settings.fan_enabled = True
    inp.fan_blocked = "Indoor humidity 68% is over the 60% limit"
    res = engine.run(inp)
    assert not res["fan"]["searched"] and res["fan"]["blocked"]
    assert not any(n.get("fan") for n in (res["best"] or res["closest"])["nodes"])
    inp = _fan_house()
    inp.settings.fan_enabled = True
    inp.fan = []  # never circulated, so the effect cannot be learned
    res = engine.run(inp)
    assert res["rooms"][0]["fan_learned"] is False and not res["fan"]["searched"]


def test_fan_skipped_when_blower_costs_more_than_it_saves():
    inp = _fan_house()
    no_fan = engine.run(inp)
    inp.settings.fan_enabled = True
    inp.settings.fan_kw = 0.5
    res = engine.run(inp)
    assert res["fan"]["searched"]
    assert res["best"]["energy_index"] <= no_fan["best"]["energy_index"] + 1e-9
    if not any(n.get("fan") for n in res["best"]["nodes"]):
        assert res["best"]["nodes"] == no_fan["best"]["nodes"]


# ------------------------------------------------------------------ thermostat switching band
def test_learns_thermostat_that_cools_below_setpoint():
    # The thermostat keeps cooling until the reading is 2.5 below the setpoint (remote sensors, say).
    inp, truth = _synthetic(band=(-2.5, -1.5))
    plain = engine.validate(
        engine.fit_model(*_fit_args(inp)), *_val_args(inp)
    )
    r = engine.run(inp)
    c = r["control"]
    assert c["source"] == "learned"
    assert -3.2 < c["off_at"] < -2.2 and -2.2 < c["on_at"] < -1.0
    assert plain["ratio"] < 0.9  # the old symmetric band under-predicts runtime
    assert 0.85 < r["validation"]["ratio"] < 1.15
    assert r["control"]["setpoint_shift"] >= 1.0  # proposes higher numbers for the same comfort


def _hist(inp):
    Ti = engine.resample(inp.indoor, inp.g0, inp.n_hist, inp.data_end)
    on = engine.resample(inp.on, inp.g0, inp.n_hist, inp.data_end)
    sp = engine.ffill(engine.resample(inp.setpoint, inp.g0, inp.n_hist, inp.data_end))
    ts = [inp.g0 + i * STEP for i in range(inp.n_hist)]
    To = [engine.interp(t, inp.weather_ts, inp.weather_temp) for t in ts]
    S = [engine.interp(t, inp.weather_ts, inp.weather_sun) for t in ts]
    return Ti, on, sp, To, S


def _fit_args(inp):
    Ti, on, sp, To, S = _hist(inp)
    return Ti, To, S, on, inp.settings


def _val_args(inp):
    Ti, on, sp, To, S = _hist(inp)
    return Ti, on, sp, To, S, inp.n_hist, inp.settings


def test_too_few_switches_keeps_default_band():
    s = engine.Settings.defaults(True)
    c = engine.learn_control([(0, 74.0), (900, 75.0)], [(0, 0.0), (450, 1.0)], [(0, 74.0)], s)
    assert c["source"] == "default"


def test_start_right_after_setpoint_drop_is_ignored():
    s = engine.Settings.defaults(True)
    s.min_control_events = 1
    indoor = [(i * 60, 75.0) for i in range(400)]
    sp = [(0, 75.0), (10000, 72.0)]
    # normal cycle at +0.5 / -0.5, then a forced start the moment the setpoint drops (reading 3 above)
    on = [(0, 0.0), (2000, 1.0), (4000, 0.0), (10010, 1.0), (16000, 0.0)]
    pts = [(t, 75.0 + (0.5 if t == 2000 else 0.0)) for t, _ in indoor]
    c = engine.learn_control(pts, on, sp, s)
    assert c["starts"] == 1  # the 10010 start is skipped


# ------------------------------------------------------------------ residual layer
def test_learns_ac_losing_capacity_on_hot_days():
    inp, truth = _synthetic(hot=6.0, weak_above=90.0, weak=0.35, noise=0.03)
    r = engine.run(inp)
    res = r["residual"]
    assert res["used"], res
    assert res["coef"]["capacity"] > 0
    assert res["rmse_with"] < res["rmse_without"]


def test_residual_rejected_when_model_is_already_right():
    inp, _ = _synthetic()
    r = engine.run(inp)
    assert not r["residual"]["used"]


# ------------------------------------------------------------------ per-bin limits and learned comfort
def test_learned_rule_tightens_only_its_window():
    s = engine.Settings.defaults(True)
    lm = [h * 60 for h in range(24)]
    rule = {"kind": "house", "start": 21 * 60, "end": 22 * 60, "max": 74.0, "cond": None}
    ctx = engine.make_ctx([80.0] * 24, [0.0] * 24, lm, s, learned=[rule])
    assert ctx["lim"][1 + 21] == 74.0
    assert ctx["lim"][1 + 20] == s.awake_max
    hot = {"kind": "house", "start": 0, "end": 0, "max": 74.0, "cond": {"outdoor_min": 90.0}}
    ctx = engine.make_ctx([80.0] * 12 + [95.0] * 12, [0.0] * 24, lm, s, learned=[hot])
    assert ctx["lim"][1 + 10] == s.awake_max and ctx["lim"][1 + 14] == 74.0


def _ev(day, hour, frm=74.0, to=72.0, indoor=74.5, rooms=None, outdoor=85.0, humidity=45.0):
    ts = 1_792_000_000 - 30 * 86400 + day * 86400 + hour * 3600
    return {"ts": ts, "lm": hour * 60, "from": frm, "to": to, "indoor": indoor, "rooms": rooms or {},
            "outdoor": outdoor, "humidity": humidity, "source": "ui"}


def test_comfort_rule_needs_two_days():
    s = engine.Settings.defaults(True)
    now = 1_792_000_000 - 30 * 86400 + 20 * 86400
    room = {"sensor.bed": {"t": 76.5, "trend": 0.2}}
    one = engine.learn_comfort([_ev(18, 21, rooms=room)], now, s, None, {"sensor.bed"})
    assert not one["rules"] and one["pending"]
    two = engine.learn_comfort([_ev(16, 21, rooms=room), _ev(18, 21, rooms=room)], now, s, None, {"sensor.bed"})
    rule = two["rules"][0]
    assert rule["kind"] == "room" and rule["room"] == "sensor.bed"
    assert rule["max"] == 75.5 and rule["start"] == 20 * 60
    assert rule["max"] <= s.awake_max


def test_comfort_causes_hot_humid_and_never_loosens():
    s = engine.Settings.defaults(True)
    now = 1_792_000_000 - 30 * 86400 + 20 * 86400
    hot = engine.learn_comfort([_ev(15, 17, outdoor=96), _ev(17, 17, outdoor=97)], now, s, 92.0, set())
    assert hot["rules"][0]["cause"] == "hot" and hot["rules"][0]["cond"]["outdoor_min"] == 94.0
    humid = engine.learn_comfort([_ev(15, 17, humidity=66), _ev(17, 17, humidity=64)], now, s, 92.0, set())
    assert humid["rules"][0]["cause"] == "humid"
    # turned down while already well inside the limit: nothing to tighten
    cool = engine.learn_comfort([_ev(15, 17, indoor=70.0), _ev(17, 17, indoor=70.0)], now, s, None, set())
    assert all(r["max"] >= s.awake_max - s.learn_max_drop for r in cool["rules"])
    # never more than learn_max_drop below the configured limit
    cold = engine.learn_comfort([_ev(15, 17, indoor=60.0), _ev(17, 17, indoor=60.0)], now, s, None, set())
    assert cold["rules"][0]["max"] == s.awake_max - s.learn_max_drop


def test_comfort_rules_fade_and_turn_ups_count_against():
    s = engine.Settings.defaults(True)
    base = 1_792_000_000 - 30 * 86400
    evs = [_ev(0, 21), _ev(2, 21)]
    assert engine.learn_comfort(evs, base + 4 * 86400, s, None, set())["rules"]
    assert not engine.learn_comfort(evs, base + 27 * 86400, s, None, set())["rules"]  # faded
    ups = evs + [_ev(3, 20, frm=72, to=74), _ev(4, 20, frm=72, to=74)]
    out = engine.learn_comfort(ups, base + 5 * 86400, s, None, set())
    assert not out["rules"] and out["notes"]
    forgot = engine.learn_comfort(evs, base + 4 * 86400, s, None, set(), {"time::awake": base + 3 * 86400})
    assert not forgot["rules"] and not forgot["pending"]


def test_learned_rule_changes_the_proposal():
    inp, _ = _synthetic()
    plain = engine.run(inp)
    now = inp.data_end
    evs = [
        {"ts": now - d * 86400, "lm": 15 * 60, "from": 76, "to": 73, "indoor": 74.0, "rooms": {},
         "outdoor": 80, "humidity": 40, "source": "ui"}
        for d in (3, 1)
    ]
    inp.comfort_events = evs
    r = engine.run(inp)
    rules = r["comfort_learned"]["rules"]
    assert rules and rules[0]["max"] == 73.0
    assert r["best"]["temp_max"] <= plain["best"]["temp_max"]
    assert r["series"]["limit"][r["series"]["now_index"]:].count(73.0) > 0


# ------------------------------------------------------------------ free-form search
def test_refine_respects_change_limits_and_is_deterministic():
    inp, _ = _synthetic()
    a = engine.run(inp)
    b = engine.run(inp)
    assert a["best"] == b["best"]
    nodes = a["best"]["nodes"]
    mins = sorted(engine.to_minutes(n["time"]) for n in nodes)
    assert len(nodes) <= inp.settings.max_nodes
    if len(mins) > 1:
        gaps = [y - x for x, y in zip(mins, mins[1:])] + [1440 - mins[-1] + mins[0]]
        assert min(gaps) >= inp.settings.min_gap
    assert a["search"]["refine_evals"] <= inp.settings.max_evals


def test_refine_finds_precool_when_ac_is_weak_in_the_afternoon():
    s = engine.Settings.defaults(True)
    lm = [(i * 15) % 1440 for i in range(96)]
    To = [80 + 18 * max(0.0, math.sin(math.pi * (m / 60 - 9) / 12)) for m in lm]
    S = [max(0.0, 900 * math.sin(math.pi * (m / 60 - 6) / 12)) for m in lm]
    m = {"k": 0.09, "g": 1.1, "q": 0.15, "c": 2.0}  # an AC that cannot keep up at the peak
    ctx = engine.make_ctx(To, S, lm, s)
    grid_best = None
    for nodes in engine.candidates(s):
        r = engine.evaluate(m, nodes, ctx, 71.0, 0, s)
        if engine._better(r, grid_best, s):
            grid_best = r
    assert grid_best["viol"] > 0  # no day/night shape holds the afternoon
    best, evals = engine.refine(m, [(grid_best["nodes"], None)], ctx, 71.0, 0, s, 66.0, 77.0)
    assert best["viol"] == 0.0
    noon = lambda nodes: engine.setpoint_at([{"time": engine.hhmm(mn), "temp": t} for mn, t in nodes], 12 * 60)
    assert noon(best["nodes"]) < noon(grid_best["nodes"])  # cools ahead of the hot hours
    assert evals <= s.max_evals


def test_previous_proposal_is_kept_when_close():
    inp, _ = _synthetic()
    first = engine.run(inp)
    inp.previous_nodes = first["best"]["nodes"]
    again = engine.run(inp)
    assert again["best"]["nodes"] == first["best"]["nodes"]
    # a previous proposal that now breaks comfort is dropped
    inp.previous_nodes = [{"time": "00:00", "temp": 80}]
    r = engine.run(inp)
    assert not r["search"]["kept_previous"] and r["best"]["nodes"] != inp.previous_nodes


# ------------------------------------------------------------------ stored forecasts
def test_past_plan_series_and_accuracy():
    inp, _ = _synthetic()
    r0 = engine.run(inp)
    s0 = r0["series"]
    start = s0["start"]
    plan = {start + i * STEP: {"in": v + 0.5, "rooms": {}} for i, v in enumerate(s0["indoor_actual"][: s0["now_index"]]) if v is not None}
    inp.past_plan = plan
    r = engine.run(inp)
    s = r["series"]
    assert s["indoor_planned"][: s["now_index"]].count(None) < 5
    assert all(v is None for v in s["indoor_planned"][s["now_index"]:])
    assert abs(s["accuracy"]["bias"] + 0.5) < 0.06 and abs(s["accuracy"]["mae"] - 0.5) < 0.06
    bins = r["plan_bins"]
    assert len(bins) == inp.n_hor and bins[0]["ts"] == start + s["now_index"] * STEP
    assert {"in", "sp", "ac", "to", "sun", "rooms"} <= set(bins[0])


def test_accuracy_needs_enough_pairs():
    assert engine.accuracy([70.0] * 10, [71.0] * 10) is None
    acc = engine.accuracy([70.0] * 30, [71.0] * 30)
    assert acc == {"mae": 1.0, "bias": -1.0, "n": 30}


def test_quick_refresh_grid_is_smaller_and_still_comfortable():
    inp, _ = _synthetic()
    full = engine.run(inp)
    inp.quick = True
    quick = engine.run(inp)
    assert quick["candidates"] < full["candidates"] / 5
    assert quick["best"] is not None and quick["best"]["overshoot"] == 0
    assert quick["best"]["energy_index"] <= full["best"]["energy_index"] * 1.03


# ------------------------------------------------------------------ heating
def _winter(history_days=14, noise=0.05, seed=11, mode="heat", kind="furnace", cold=40.0, hband=(0.6, -0.6), room_cold=0.0):
    """A house heated by a furnace (h) in cold weather. mode "heat_cool" also keeps a cool setpoint far above."""
    rng = random.Random(seed)
    s = engine.Settings.defaults(True)
    s.mode = mode
    s.heat_kind = kind
    n_hist = history_days * 96
    t0 = 1_792_000_000 - (1_792_000_000 % STEP)
    g0 = t0 - n_hist * STEP
    ts = [g0 + i * STEP for i in range(n_hist + 96)]

    def wxf(t):
        temp, sun = _weather(t)
        return temp - cold, sun * 0.6

    wx = [wxf(t) for t in ts]
    truth = {"k": 0.09, "g": 1.1, "q": 0.15, "c": 3.2, "h": 6.0}
    from bisect import bisect_right

    hm = [m for m, _ in s.heat_baseline]
    ht = [t for _, t in s.heat_baseline]
    sph = [ht[bisect_right(hm, _local_min(t)) - 1] for t in ts[:n_hist]]
    cool = [80.0 if mode == "heat_cool" else None] * n_hist
    Ti, on, heat = engine.simulate(
        truth, [w[0] for w in wx[:n_hist]], [w[1] for w in wx[:n_hist]], 67.0, cool, 0.75, 0, None, sph, hband, 0
    )
    first_hour = g0 - (g0 % 3600) - 3600
    wts = [first_hour + h * 3600 for h in range(int((ts[-1] - first_hour) / 3600) + 3)]
    inp = engine.Inputs(
        g0=g0, n_hist=n_hist, n_hor=96, data_end=t0,
        indoor=[(ts[i], Ti[i] + rng.gauss(0, noise)) for i in range(n_hist)],
        on=[(ts[i], float(on[i])) for i in range(n_hist)],
        setpoint=[(ts[i], cool[i]) for i in range(n_hist)],
        weather_ts=wts, weather_temp=[wxf(t)[0] for t in wts], weather_sun=[wxf(t)[1] for t in wts],
        local_minutes=[_local_min(t) for t in ts[n_hist:]], settings=s,
        heat=[(ts[i], float(heat[i])) for i in range(n_hist)],
        heat_setpoint=[(ts[i], sph[i]) for i in range(n_hist)],
        modes=[(g0, mode)],
    )
    if room_cold:
        # a room that drops room_cold degrees below the thermostat while the furnace runs (duty over the hour)
        pts = []
        for i in range(n_hist):
            duty = sum(heat[max(0, i - 4):i]) / 4 if i else 0.0
            pts.append((ts[i], Ti[i] - 0.5 - room_cold * duty + rng.gauss(0, 0.05)))
        inp.rooms = [{"id": "sensor.kids", "name": "Kids", "when": "always", "points": pts}]
    return inp, truth


def test_heat_mode_learns_furnace_and_keeps_minimums():
    inp, truth = _winter()
    r = engine.run(inp)
    assert r["mode"] == "heat" and r["heat_learned"]
    assert abs(r["model"]["h"] - truth["h"]) < 1.0
    assert 0.8 < r["validation"]["heat_ratio"] < 1.2
    assert r["status"] == "ready", r["status"]
    nodes = r["best"]["nodes"]
    assert all("heat" in n for n in nodes)
    pred = r["series"]["indoor"]["proposed"][r["series"]["now_index"]:]
    lim = r["series"]["limit_min"][r["series"]["now_index"]:]
    assert all(p is None or l is None or p >= l - 0.3 for p, l in zip(pred[1:], lim))
    assert r["runtime"]["kind"] == "heat" and r["runtime"]["proposed"]["hours"] > 0
    assert all(v is None for v in r["series"]["limit"][r["series"]["now_index"]:])  # no cooling limit in heat mode


def test_heat_mode_without_heating_history_says_so():
    inp, _ = _synthetic()
    inp.settings.mode = "heat"
    try:
        engine.run(inp)
        raise AssertionError("expected a ModelError")
    except engine.ModelError as err:
        assert "heat" in str(err)


def test_heat_pump_avoids_deep_setbacks_that_need_backup_strips():
    def depth(kind):
        inp, _ = _winter(kind=kind, cold=45.0)
        inp.settings.aux_ratio = 4.0
        inp.settings.aux_below = 0.0  # strips only for big warm-ups here
        inp.settings.heat_ratio = 0.9 if kind == "heat_pump" else 1.5
        r = engine.run(inp)
        heats = [n["heat"] for n in r["best"]["nodes"]]
        return max(heats) - min(heats), r

    furnace, rf = depth("furnace")
    pump, rp = depth("heat_pump")
    assert pump <= furnace
    assert rf["heat_kind"] == "furnace" and rp["heat_kind"] == "heat_pump"


def test_heat_cost_by_kind():
    s = engine.Settings.defaults(True)
    s.heat_kind = "heat_pump"
    assert engine.heat_cost(30.0, 0.5, s) == s.aux_ratio  # cold outside: strips
    assert engine.heat_cost(50.0, 3.0, s) == s.aux_ratio  # big warm-up: strips
    assert s.heat_ratio <= engine.heat_cost(40.0, 0.5, s) < s.aux_ratio
    s.heat_kind = "dual_fuel"
    assert engine.heat_cost(30.0, 3.0, s) == s.gas_ratio
    s.heat_kind = "furnace"
    assert engine.heat_cost(10.0, 5.0, s) == s.heat_ratio


def test_heat_cool_keeps_the_deadband_and_both_setpoints():
    inp, _ = _winter(mode="heat_cool", cold=20.0)
    r = engine.run(inp)
    assert r["mode"] == "heat_cool"
    nodes = r["best"]["nodes"]
    assert all("heat" in n for n in nodes)
    assert all(n["heat"] <= n["temp"] - inp.settings.min_deadband for n in nodes)
    assert r["series"]["limit"][r["series"]["now_index"]] is not None or r["series"]["limit_min"][r["series"]["now_index"]] is not None


def test_cool_mode_passes_heat_values_through_untouched():
    inp, _ = _synthetic()
    inp.active_nodes = [{"time": "08:00", "temp": 75, "heat": 66}, {"time": "22:00", "temp": 72, "heat": 63}]
    r = engine.run(inp)
    heats = {n.get("heat") for n in r["best"]["nodes"]}
    assert heats <= {66, 63}  # cooling search never invents heat values


def test_room_learns_it_runs_cold_while_heating():
    inp, _ = _winter(room_cold=2.0)
    r = engine.run(inp)
    room = r["rooms"][0]
    assert room["heat_learned"] and room["coef"]["heat"] < -1.0


def test_merge_all_and_heat_plan():
    nodes = [{"time": "06:00", "temp": 76, "heat": 68}, {"time": "22:00", "temp": 74, "heat": 64}]
    hp = engine.heat_plan(nodes)
    assert hp == [(360, 68.0), (1320, 64.0)]
    merged = engine.merge_all([(360, 76.0), (1320, 74.0)], None, [(300, 68.0), (1320, 64.0)])
    assert merged[0] == (300, 74.0, False, 68.0) and merged[-1] == (1320, 74.0, False, 64.0)
    assert engine.heat_plan([{"time": "06:00", "temp": 76}]) is None


def test_comfort_heat_side_learns_from_turn_ups():
    s = engine.Settings.defaults(True)
    s.mode = "heat"
    now = 1_792_000_000 - 30 * 86400 + 20 * 86400
    ev = lambda d, h, frm, to, **kw: {**_ev(d, h, frm=frm, to=to, indoor=66.5, **kw), "side": "heat"}
    # 6 AM sits between the sleep and awake hours, so only the lower sleep minimum applies there
    out = engine.learn_comfort([ev(15, 6, 66, 69), ev(17, 6, 66, 69)], now, s, None, set())
    rule = out["rules"][0]
    assert rule["side"] == "min" and rule["cause"] == "cold_time" and rule["max"] == 67.5
    assert rule["start"] == 5 * 60
    # at 7 AM the configured 68 awake minimum already covers it: nothing to tighten
    assert not engine.learn_comfort([ev(15, 7, 66, 69), ev(17, 7, 66, 69)], now, s, None, set())["rules"]
    room = {"sensor.kids": {"t": 68.0, "trend": -0.2}}
    warm = lambda d: {**ev(d, 21, 68, 70, rooms=room), "indoor": 70.0}
    out = engine.learn_comfort([warm(15), warm(17)], now, s, None, {"sensor.kids"})
    assert out["rules"][0]["cause"] == "cold_room" and out["rules"][0]["max"] == 69.0
    down = engine.learn_comfort([ev(15, 13, 70, 68), ev(17, 13, 70, 68)], now, s, None, set())
    assert not down["rules"] and down["notes"][0]["side"] == "heat"
    # a learned minimum raises the heating limit only inside its window
    ctx = engine.make_ctx([40.0] * 24, [0.0] * 24, [h * 60 for h in range(24)], s, learned=[{**rule, "kind": "house", "start": 6 * 60, "end": 9 * 60, "max": 70.0}])
    assert ctx["lim_min"][1 + 7] == 70.0 and ctx["lim_min"][1 + 12] == s.awake_min
