"""Pure Python thermal model and schedule optimizer.

No Home Assistant imports in this file, so it can be unit tested anywhere.

Model (one node, cooling only):

    dTin/dt = k * (Tout - Tin) + g * Sun/1000 + q - c * AC_on

    k  leakiness of the house (1/hr)
    g  solar gain (deg per hr per kW/m2 of sunshine)
    q  steady internal heat (people, servers, appliances), deg per hr
    c  how fast the AC pulls the house down while running, deg per hr

The model is fit by least squares on the thermostat's own history, checked
against the last week of real AC runtime, then used to simulate a few thousand
candidate daily schedules against the forecast.

Rooms (optional): each extra indoor sensor gets a static offset model against
the thermostat reading,

    Troom - Tin = a + b * (Tout - Tin) + c * Sun/1000 + d * AC_recent

where AC_recent is the AC duty over the last hour. Trusted rooms add their own
comfort limits to the search.

Outdoor sensors (optional): their median replaces the Open-Meteo past weather
for the fit, and the average sensor-minus-forecast gap for each local hour
corrects the forecast.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

STEP_S = 900
STEP_H = STEP_S / 3600.0


class ModelError(Exception):
    """Raised when the history cannot support a model fit."""


@dataclass
class Settings:
    """Comfort limits and tuning. Temperatures are in the Home Assistant unit."""

    fahrenheit: bool = True
    awake_start: int = 7
    awake_end: int = 22
    awake_max: float = 76.0
    sleep_start: int = 23
    sleep_end: int = 6
    sleep_max: float = 72.5
    hysteresis: float = 0.75
    step: float = 1.0
    baseline: list = field(default_factory=lambda: [(8 * 60, 74.0), (22 * 60, 70.0)])
    energy_temp_coef: float = 0.012
    ref_outdoor: float = 82.0
    rate_limit: float = 8.0
    ratio_limits: tuple = (0.70, 1.30)
    min_rows: int = 400
    simplicity_penalty: float = 0.02
    min_room_rows: int = 288  # about 3 days of 15 minute bins
    room_rmse_max: float = 1.5  # a room model worse than this is shown but does not limit the search
    max_outdoor_bias: float = 10.0

    @classmethod
    def defaults(cls, fahrenheit: bool = True) -> "Settings":
        if fahrenheit:
            return cls(fahrenheit=True)
        return cls(
            fahrenheit=False,
            awake_max=24.5,
            sleep_max=22.5,
            hysteresis=0.4,
            step=0.5,
            baseline=[(8 * 60, 23.5), (22 * 60, 21.0)],
            ref_outdoor=27.8,
            rate_limit=4.5,
            room_rmse_max=0.8,
            max_outdoor_bias=5.5,
        )


@dataclass
class Inputs:
    g0: int  # epoch of first history bin
    n_hist: int  # number of 15 minute history bins
    n_hor: int  # number of 15 minute forecast bins
    data_end: int  # epoch where history ends (now)
    indoor: list  # [(epoch, temp or None)]
    on: list  # [(epoch, 1.0 cooling / 0.0 not / None unknown)]
    setpoint: list  # [(epoch, setpoint or None)]
    weather_ts: list  # hourly epochs, ascending
    weather_temp: list
    weather_sun: list  # shortwave radiation W/m2
    local_minutes: list  # local minute-of-day for each forecast bin
    settings: Settings
    local_minutes_past: list = field(default_factory=list)  # same, for the 24h before now (for the card)
    active_nodes: list | None = None  # schedule currently running, [{"time","temp"}]
    rooms: list = field(default_factory=list)  # [{"id","name","when","points": [(epoch, temp or None)]}]
    outdoor: list = field(default_factory=list)  # one [(epoch, temp or None)] list per outdoor sensor


# ------------------------------------------------------------------ helpers
def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _fmt_temp(t: float) -> float | int:
    t = round(float(t), 2)
    return int(t) if t == int(t) else t


def _snap(x: float, step: float) -> float:
    return round(round(x / step) * step, 3)


def interp(x: float, xs: list, ys: list) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_right(xs, x)
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def resample(points: list, g0: int, n: int, end_ts: float, step: int = STEP_S) -> list:
    """Time weighted mean of a step function onto n bins of `step` seconds from g0.

    points: [(epoch, value or None)] sorted by time. A value holds until the next
    point (or end_ts for the last one). Bins with under 50% coverage give None.
    """
    acc = [0.0] * n
    cov = [0.0] * n
    m = len(points)
    limit = g0 + n * step
    for idx in range(m):
        t0, v = points[idx]
        t1 = points[idx + 1][0] if idx + 1 < m else end_ts
        if v is None or t1 <= t0:
            continue
        lo, hi = max(t0, g0), min(t1, limit)
        if hi <= lo:
            continue
        b = int((lo - g0) // step)
        while b < n:
            b_start = g0 + b * step
            if b_start >= hi:
                break
            seg = min(hi, b_start + step) - max(lo, b_start)
            if seg > 0:
                acc[b] += v * seg
                cov[b] += seg
            b += 1
    return [acc[i] / cov[i] if cov[i] >= 0.5 * step else None for i in range(n)]


def ffill(a: list) -> list:
    out, last = [], None
    for v in a:
        if v is not None:
            last = v
        out.append(last)
    return out


def _solve(A: list, b: list) -> list:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            raise ModelError("History does not contain enough variation to fit the model")
        M[c], M[p] = M[p], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            for k in range(c, n + 1):
                M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x


# ------------------------------------------------------------------ schedule helpers
def to_minutes(hhmm_str: str) -> int:
    parts = str(hhmm_str).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def parse_nodes(nodes: list) -> list[tuple[int, float]]:
    """[{"time": "08:00", "temp": 74}] -> sorted [(minute_of_day, temp)]."""
    out = sorted((to_minutes(n["time"]), float(n["temp"])) for n in nodes)
    if not out:
        raise ValueError("A schedule needs at least one node")
    return out


def setpoint_at(nodes: list, minute: int) -> float:
    """Setpoint active at a local minute-of-day. Before the first node, the last node (overnight) holds."""
    parsed = parse_nodes(nodes)
    mins = [p[0] for p in parsed]
    return parsed[bisect_right(mins, minute) - 1][1]


def next_change(nodes: list, minute: int) -> tuple[int, float]:
    """Minutes until the next node fires (1..1440) and the setpoint it sets."""
    parsed = parse_nodes(nodes)
    for m, t in parsed:
        if m > minute:
            return m - minute, t
    first_m, first_t = parsed[0]
    return 1440 - minute + first_m, first_t


# ------------------------------------------------------------------ model
def fit_model(Ti: list, To: list, S: list, on: list, s: Settings) -> dict:
    rows = []
    for i in range(len(Ti) - 1):
        a, b, o, t, sun = Ti[i], Ti[i + 1], on[i], To[i], S[i]
        if a is None or b is None or o is None or t is None or sun is None:
            continue
        y = (b - a) / STEP_H
        if abs(y) > s.rate_limit:
            continue
        rows.append(((t - a, sun / 1000.0, 1.0, -o), y))
    if len(rows) < s.min_rows:
        raise ModelError(
            f"Only {len(rows)} usable history rows (need {s.min_rows}). "
            "Give the recorder a few more days of AC history."
        )
    XtX = [[0.0] * 4 for _ in range(4)]
    Xty = [0.0] * 4
    for x, y in rows:
        for i in range(4):
            Xty[i] += x[i] * y
            for j in range(4):
                XtX[i][j] += x[i] * x[j]
    beta = _solve(XtX, Xty)
    ybar = sum(y for _, y in rows) / len(rows)
    ss_res = ss_tot = 0.0
    for x, y in rows:
        pred = sum(beta[i] * x[i] for i in range(4))
        ss_res += (y - pred) ** 2
        ss_tot += (y - ybar) ** 2
    m = {
        "k": beta[0],
        "g": beta[1],
        "q": beta[2],
        "c": beta[3],
        "r2": 1 - ss_res / ss_tot if ss_tot > 0 else 0.0,
        "rows": len(rows),
    }
    if m["k"] <= 0.005 or m["c"] <= 0.04 * s.rate_limit:
        raise ModelError(
            f"Fitted house model is not physical (k={m['k']:.3f}, c={m['c']:.2f}). "
            "There may not be enough AC cycling in the recent history."
        )
    return m


def simulate(m: dict, To: list, S: list, ti0: float, sp: list, hyst: float, on0: int = 0):
    """Thermostat plus house simulation on plain lists."""
    k, g, q, c = m["k"], m["g"], m["q"], m["c"]
    Ti = [ti0]
    on = []
    state = on0
    t = ti0
    for i in range(len(sp)):
        if state:
            if t < sp[i] - hyst:
                state = 0
        elif t > sp[i] + hyst:
            state = 1
        on.append(state)
        t = t + STEP_H * (k * (To[i] - t) + g * S[i] / 1000.0 + q - c * state)
        Ti.append(t)
    return Ti, on


def validate(m: dict, Ti: list, on: list, sp: list, To: list, S: list, idx0: int, s: Settings, days: int = 7) -> dict:
    """Replay recent days with the REAL setpoints and compare AC runtime."""
    chunk = 96
    pred = actual = 0.0
    used = 0
    for d in range(days):
        a = idx0 - (days - d) * chunk
        if a < 0 or Ti[a] is None:
            continue
        b = a + chunk
        seg_sp, seg_To, seg_S, seg_on = sp[a:b], To[a:b], S[a:b], on[a:b]
        if any(v is None for v in seg_sp) or any(v is None for v in seg_To) or any(v is None for v in seg_S):
            continue
        known = [v for v in seg_on if v is not None]
        if len(known) < 0.8 * chunk:
            continue
        first = seg_on[0] if seg_on[0] is not None else 0.0
        _, p = simulate(m, seg_To, seg_S, Ti[a], seg_sp, s.hysteresis, 1 if first > 0.5 else 0)
        pred += sum(p) * STEP_H
        actual += sum(known) * STEP_H * (chunk / len(known))
        used += 1
    if used == 0 or actual <= 0:
        return {"days": 0, "predicted_hours": None, "actual_hours": None, "ratio": None}
    return {"days": used, "predicted_hours": pred / used, "actual_hours": actual / used, "ratio": pred / actual}


# ------------------------------------------------------------------ rooms and outdoor sensors
ROOM_WHEN = ("always", "awake", "asleep", "never")


def _duty(on: list, i: int) -> float | None:
    """AC duty over the hour before Ti index i (bins i-4 .. i-1)."""
    vals = [v for v in on[max(0, i - 4) : i] if v is not None]
    return sum(vals) / len(vals) if vals else None


def fit_room(R: list, Ti: list, To: list, S: list, on: list, s: Settings) -> dict:
    """Least squares fit of the room offset against the thermostat reading."""
    rows = []
    for i in range(len(R)):
        r, t, o, sun = R[i], Ti[i], To[i], S[i]
        d = _duty(on, i) if i > 0 else on[0]
        if r is None or t is None or o is None or sun is None or d is None:
            continue
        rows.append(((1.0, o - t, sun / 1000.0, d), r - t))
    out = {"rows": len(rows), "coef": None, "rmse": None, "r2": None, "trusted": False}
    if len(rows) < s.min_room_rows:
        out["reason"] = f"Learning: {len(rows)} of {s.min_room_rows} readings"
        return out
    lam = 1e-3 * len(rows)  # light ridge so a feature that never moves (no sun at night) cannot blow up
    XtX = [[0.0] * 4 for _ in range(4)]
    Xty = [0.0] * 4
    for x, y in rows:
        for i in range(4):
            Xty[i] += x[i] * y
            for j in range(4):
                XtX[i][j] += x[i] * x[j]
    for i in range(1, 4):
        XtX[i][i] += lam
    try:
        beta = _solve(XtX, Xty)
    except ModelError:
        out["reason"] = "Readings do not vary enough to learn this room yet"
        return out
    ybar = sum(y for _, y in rows) / len(rows)
    ss_res = sum((y - sum(b * v for b, v in zip(beta, x))) ** 2 for x, y in rows)
    ss_tot = sum((y - ybar) ** 2 for _, y in rows)
    out["coef"] = beta
    out["rmse"] = math.sqrt(ss_res / len(rows))
    out["r2"] = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    out["trusted"] = out["rmse"] <= s.room_rmse_max
    out["reason"] = None if out["trusted"] else "Room tracks the thermostat too loosely to plan around"
    return out


def room_trace(coef: list, Ti: list, on: list, To: list, S: list, on0: int) -> list:
    """Predicted room temperature for each Ti index (Ti has one more element than on/To/S)."""
    a, b, c, d = coef
    n = len(To)
    out = []
    for i, t in enumerate(Ti):
        j = min(i, n - 1)
        duty = _duty(on, i) if i > 0 else float(on0)
        out.append(t + a + b * (To[j] - t) + c * S[j] / 1000.0 + d * duty)
    return out


def median_series(series: list) -> list:
    out = []
    for vals in zip(*series):
        v = sorted(x for x in vals if x is not None)
        if not v:
            out.append(None)
        elif len(v) % 2:
            out.append(v[len(v) // 2])
        else:
            out.append((v[len(v) // 2 - 1] + v[len(v) // 2]) / 2)
    return out


def outdoor_bias(sensor: list, forecast: list, local_minutes: list, s: Settings) -> list:
    """Average sensor minus forecast for each local hour, clamped. 0 where there is too little data."""
    acc = [0.0] * 24
    cnt = [0] * 24
    for v, w, lm in zip(sensor, forecast, local_minutes):
        if v is None or w is None:
            continue
        acc[lm // 60] += v - w
        cnt[lm // 60] += 1
    lim = s.max_outdoor_bias
    return [max(-lim, min(lim, acc[h] / cnt[h])) if cnt[h] >= 8 else 0.0 for h in range(24)]


# ------------------------------------------------------------------ optimizer
def candidates(s: Settings):
    st = s.step
    day_t = [_snap(s.awake_max - st * i, st) for i in range(4, -1, -1)]
    night_top = math.floor(s.sleep_max / st + 1e-9) * st
    night_t = [_snap(night_top - st * i, st) for i in range(5, -1, -1)]
    wakes = sorted({h for h in (s.awake_start - 1, s.awake_start, s.awake_start + 1) if 0 <= h <= 23})
    sleeps = sorted({h for h in (s.awake_end - 1, s.awake_end, s.awake_end + 1) if 0 <= h <= 23})
    for Td in day_t:
        for Tn in night_t:
            for wake in wakes:
                for sleep in sleeps:
                    yield [(wake * 60, Td), (sleep * 60, Tn)]
                    for bump in (st, 2 * st):
                        for cs in (14, 15, 16):
                            for ce in (18, 19, 20):
                                if wake < cs and ce < sleep:
                                    yield [
                                        (wake * 60, Td),
                                        (cs * 60, _snap(Td + bump, st)),
                                        (ce * 60, Td),
                                        (sleep * 60, Tn),
                                    ]


def make_ctx(To_h: list, S_h: list, local_minutes: list, s: Settings, rooms: list | None = None) -> dict:
    """rooms: [{"coef", "when"}] for the trusted rooms that limit the search."""
    awake, sleep = [], []
    for j, lm in enumerate(local_minutes):
        h = lm // 60
        if s.awake_start <= h < s.awake_end:
            awake.append(j + 1)  # +1: Ti has one extra leading element
        if h >= s.sleep_start or h < s.sleep_end:
            sleep.append(j + 1)
    return {"To": To_h, "S": S_h, "lm": local_minutes, "awake": awake, "sleep": sleep, "rooms": rooms or []}


def evaluate(m: dict, nodes: list, ctx: dict, ti0: float, on0: int, s: Settings, keep: bool = False) -> dict:
    mins = [x[0] for x in nodes]
    temps = [x[1] for x in nodes]
    sp = [temps[bisect_right(mins, x) - 1] for x in ctx["lm"]]  # index -1 wraps to last node (overnight)
    Ti, on = simulate(m, ctx["To"], ctx["S"], ti0, sp, s.hysteresis, on0)
    energy = sum(
        o * STEP_H * max(0.5, 1 + s.energy_temp_coef * (t - s.ref_outdoor)) for o, t in zip(on, ctx["To"])
    )
    va = max((Ti[i] - s.awake_max for i in ctx["awake"]), default=-99.0)
    vs = max((Ti[i] - s.sleep_max for i in ctx["sleep"]), default=-99.0)
    for room in ctx["rooms"]:
        Tr = room_trace(room["coef"], Ti, on, ctx["To"], ctx["S"], on0)
        if room["when"] in ("always", "awake"):
            va = max(va, max((Tr[i] - s.awake_max for i in ctx["awake"]), default=-99.0))
        if room["when"] in ("always", "asleep"):
            vs = max(vs, max((Tr[i] - s.sleep_max for i in ctx["sleep"]), default=-99.0))
    out = {
        "nodes": nodes,
        "energy": energy,
        "hours": sum(on) * STEP_H,
        "tmin": min(Ti),
        "tmax": max(Ti),
        "viol": max(0.0, va, vs),
        "score": energy + s.simplicity_penalty * (len(nodes) - 2),
    }
    if keep:
        out["Ti"] = Ti[:-1]  # one value per forecast bin
        out["on"] = on
        out["Tfull"] = Ti
    return out


def _pack(r: dict | None) -> dict | None:
    if r is None:
        return None
    return {
        "nodes": [{"time": hhmm(mn), "temp": _fmt_temp(t)} for mn, t in r["nodes"]],
        "ac_hours": round(r["hours"], 2),
        "energy_index": round(r["energy"], 3),
        "temp_min": round(r["tmin"], 1),
        "temp_max": round(r["tmax"], 1),
        "overshoot": round(r["viol"], 2),
    }


def run(inp: Inputs) -> dict[str, Any]:
    s = inp.settings
    n = inp.n_hist + inp.n_hor
    ts = [inp.g0 + i * STEP_S for i in range(n)]

    pad = [None] * inp.n_hor
    Ti = resample(inp.indoor, inp.g0, inp.n_hist, inp.data_end) + pad
    on = resample(inp.on, inp.g0, inp.n_hist, inp.data_end) + pad
    sp = ffill(resample(inp.setpoint, inp.g0, inp.n_hist, inp.data_end)) + pad
    To = [interp(t, inp.weather_ts, inp.weather_temp) for t in ts]
    S = [interp(t, inp.weather_ts, inp.weather_sun) for t in ts]
    idx0 = inp.n_hist

    # Local minute-of-day for every bin, history included (counted back from the forecast start).
    lm0 = inp.local_minutes[0] if inp.local_minutes else (ts[idx0] // 60) % 1440
    lm_all = [(lm0 + (i - idx0) * (STEP_S // 60)) % 1440 for i in range(n)]

    outdoor_info = None
    if inp.outdoor:
        sens = median_series([resample(p, inp.g0, inp.n_hist, inp.data_end) for p in inp.outdoor])
        bias = outdoor_bias(sens, To[:idx0], lm_all[:idx0], s)
        used = sum(1 for v in sens if v is not None)
        To = [
            (sens[i] if i < idx0 and sens[i] is not None else To[i] + bias[lm_all[i] // 60]) for i in range(n)
        ]
        outdoor_info = {
            "sensors": len(inp.outdoor),
            "history_coverage": round(used / idx0, 3) if idx0 else 0.0,
            "forecast_bias_by_hour": [round(b, 2) for b in bias],
        }

    m = fit_model(Ti[:idx0], To[:idx0], S[:idx0], on[:idx0], s)
    val = validate(m, Ti, on, sp, To, S, idx0, s)

    known = [i for i in range(idx0) if Ti[i] is not None]
    if not known:
        raise ModelError("No recent indoor temperature readings in the recorder")
    ti0 = Ti[known[-1]]
    onk = [i for i in range(idx0) if on[i] is not None]
    on0 = 1 if onk and on[onk[-1]] > 0.5 else 0

    rooms = []
    for room in inp.rooms:
        R = resample(room["points"], inp.g0, inp.n_hist, inp.data_end)
        fit = fit_room(R, Ti[:idx0], To[:idx0], S[:idx0], on[:idx0], s)
        rk = [i for i in range(idx0) if R[i] is not None]
        rooms.append({**{k: room.get(k) for k in ("id", "name", "when")}, **fit, "R": R, "now": R[rk[-1]] if rk else None})
    limiting = [
        {"coef": r["coef"], "when": r["when"]} for r in rooms if r["trusted"] and r["when"] != "never"
    ]

    ctx = make_ctx(To[idx0:], S[idx0:], inp.local_minutes, s, limiting)
    base_nodes = sorted((int(a), float(b)) for a, b in s.baseline)
    base = evaluate(m, base_nodes, ctx, ti0, on0, s, keep=True)
    active = None
    if inp.active_nodes:
        active = evaluate(m, parse_nodes(inp.active_nodes), ctx, ti0, on0, s, keep=True)

    best = least_bad = None
    n_cand = n_ok = 0
    for nodes in candidates(s):
        n_cand += 1
        r = evaluate(m, nodes, ctx, ti0, on0, s)
        if least_bad is None or (r["viol"], r["score"]) < (least_bad["viol"], least_bad["score"]):
            least_bad = r
        if r["viol"] <= 0.0:
            n_ok += 1
            if best is None or r["score"] < best["score"]:
                best = r

    if best is not None:
        best = evaluate(m, best["nodes"], ctx, ti0, on0, s, keep=True)  # re-run to keep the traces

    ratio = val["ratio"]
    validated = ratio is not None and s.ratio_limits[0] <= ratio <= s.ratio_limits[1]
    if best is None:
        status = "no_comfortable_schedule"
    elif not validated:
        status = "needs_review"
    else:
        status = "ready"

    change = change_active = None
    if best is not None and base["energy"] > 0:
        change = 100.0 * (best["energy"] - base["energy"]) / base["energy"]
    if best is not None and active is not None and active["energy"] > 0:
        change_active = 100.0 * (best["energy"] - active["energy"]) / active["energy"]

    # Chart series for the dashboard card: 24h of history then 24h of forecast, 15 minute bins.
    past = min(96, idx0)
    a = idx0 - past
    lm_past = list(inp.local_minutes_past)[-past:] if inp.local_minutes_past else []
    if len(lm_past) != past and inp.local_minutes:
        first = inp.local_minutes[0]
        lm_past = [(first - STEP_S // 60 * (past - i)) % 1440 for i in range(past)]

    def r1(v):
        return None if v is None else round(v, 1)

    traces = {"baseline": base}
    if active is not None:
        traces["active"] = active
    if best is not None:
        traces["proposed"] = best
    series = {
        "start": ts[a],
        "step": STEP_S,
        "now_index": past,
        "local_minutes": lm_past + list(inp.local_minutes),
        "outdoor": [r1(x) for x in To[a : idx0 + inp.n_hor]],
        "indoor_actual": [r1(x) for x in Ti[a:idx0]] + [None] * inp.n_hor,
        "ac_actual": [None if v is None else round(v, 2) for v in on[a:idx0]] + [None] * inp.n_hor,
        "indoor": {k: [None] * past + [r1(x) for x in r["Ti"]] for k, r in traces.items()},
        "ac": {k: [None] * past + [int(x) for x in r["on"]] for k, r in traces.items()},
        "rooms": [],
    }
    for room in rooms:
        pred = {}
        if room["coef"] is not None:
            for k, r in traces.items():
                Tr = room_trace(room["coef"], r["Tfull"], r["on"], To[idx0:], S[idx0:], on0)[:-1]
                pred[k] = [None] * past + [r1(x) for x in Tr]
                r.setdefault("room_max", {})[room["id"]] = max(Tr)
        series["rooms"].append(
            {
                "id": room["id"],
                "name": room["name"],
                "when": room["when"],
                "trusted": room["trusted"],
                "actual": [r1(x) for x in room["R"][a:idx0]] + [None] * inp.n_hor,
                "predicted": pred,
            }
        )

    def run_stats(on_list):
        hours = sum(on_list) * STEP_H
        cycles = sum(1 for i, v in enumerate(on_list) if v and (i == 0 or not on_list[i - 1]))
        longest = cur = 0
        for v in on_list:
            cur = cur + 1 if v else 0
            longest = max(longest, cur)
        per_hour = [round(sum(on_list[h * 4 : h * 4 + 4]) * 15) for h in range(len(on_list) // 4)]
        return {
            "hours": round(hours, 2),
            "cycles": cycles,
            "longest_min": longest * 15,
            "minutes_per_hour": per_hour,  # starting at the current 15 minute bin
        }

    actual_vals = [v for v in on[a:idx0] if v is not None]
    runtime = {
        "actual_24h_hours": round(sum(actual_vals) * STEP_H, 2) if len(actual_vals) >= past * 0.8 else None,
        "start_local_minute": inp.local_minutes[0] if inp.local_minutes else 0,
    }
    for name, r in traces.items():
        runtime[name] = run_stats(r["on"])

    return {
        "status": status,
        "runtime": runtime,
        "validated": validated,
        "ok_to_apply": best is not None and validated,
        "model": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()},
        "validation": {
            k: (round(v, 3) if isinstance(v, float) else v) for k, v in val.items()
        },
        "baseline": _pack(base),
        "active": _pack(active),
        "best": _pack(best),
        "closest": _pack(least_bad) if best is None else None,
        "candidates": n_cand,
        "comfortable": n_ok,
        "energy_change_pct": round(change, 1) if change is not None else None,
        "change_vs_active_pct": round(change_active, 1) if change_active is not None else None,
        "start_temp": round(ti0, 1),
        "horizon_hours": round(inp.n_hor * STEP_H, 1),
        "unit": "°F" if s.fahrenheit else "°C",
        "comfort": {
            "awake_start": s.awake_start,
            "awake_end": s.awake_end,
            "awake_max": s.awake_max,
            "sleep_start": s.sleep_start,
            "sleep_end": s.sleep_end,
            "sleep_max": s.sleep_max,
        },
        "series": series,
        "rooms": [
            {
                "id": r["id"],
                "name": r["name"],
                "when": r["when"],
                "trusted": r["trusted"],
                "limits_search": r["trusted"] and r["when"] != "never",
                "reason": r["reason"],
                "rows": r["rows"],
                "rmse": None if r["rmse"] is None else round(r["rmse"], 2),
                "r2": None if r["r2"] is None else round(r["r2"], 3),
                "coef": None if r["coef"] is None else dict(zip(("offset", "outdoor", "sun", "ac"), (round(c, 3) for c in r["coef"]))),
                "now": r1(r["now"]),
                "max_next_24h": {k: r1(t.get("room_max", {}).get(r["id"])) for k, t in traces.items()},
            }
            for r in rooms
        ],
        "outdoor_sensors": outdoor_info,
    }
