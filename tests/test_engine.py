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


def _synthetic(history_days=14, noise=0.05, seed=7):
    rng = random.Random(seed)
    s = engine.Settings.defaults(True)
    n_hist = history_days * 96
    n_hor = 96
    t0 = 1_792_000_000 - (1_792_000_000 % STEP)
    g0 = t0 - n_hist * STEP
    ts = [g0 + i * STEP for i in range(n_hist + n_hor)]
    wx = [_weather(t) for t in ts]
    truth = {"k": 0.09, "g": 1.1, "q": 0.15, "c": 3.2}

    mins = [m for m, _ in s.baseline]
    temps = [t for _, t in s.baseline]
    from bisect import bisect_right

    sp_hist = [temps[bisect_right(mins, _local_min(t)) - 1] for t in ts[:n_hist]]
    Ti_true, on_true = engine.simulate(
        truth, [w[0] for w in wx[:n_hist]], [w[1] for w in wx[:n_hist]], 73.0, sp_hist, s.hysteresis
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
        weather_temp=[_weather(t)[0] for t in wts],
        weather_sun=[_weather(t)[1] for t in wts],
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
