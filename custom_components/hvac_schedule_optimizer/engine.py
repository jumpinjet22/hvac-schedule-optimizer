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

    Troom - Tin = a + b * (Tout - Tin) + c * Sun/1000 + d * AC_recent + e * Fan_recent

where AC_recent is the AC duty over the last hour and Fan_recent the share of
the last hour the blower ran without the AC (circulate). The fan term is only
fitted once the history holds enough fan-only time. Trusted rooms add their own
comfort limits to the search.

Fan (optional): schedule nodes can carry fan=True to circulate until the next
node. Fan windows are only tried for schedules that fail because of a room, and
blower energy is charged at fan_kw / ac_kw of an AC hour.

Outdoor sensors (optional): their median replaces the Open-Meteo past weather
for the fit, and the average sensor-minus-forecast gap for each local hour
corrects the forecast.

Thermostat band: where the AC really starts and stops relative to the setpoint is
read from the switching moments in history (a thermostat on remote sensors can
cool well below its setpoint on the reading used here). Used only if it replays
recent runtime better than the configured hysteresis.

Residual layer: what the model misses under conditions (heat soak from earlier
sun, an AC losing capacity on hot days) is learned on top, and used only if it
beats the plain model on held-out days.

Search: a grid of day/night shapes, then a free-form refinement that moves,
nudges, adds or drops one change at a time (up to max_nodes changes, min_gap
apart). Comfort limits are per bin, and rules learned from repeated manual
turn-downs can only tighten them.
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
    fan_enabled: bool = False
    fan_kw: float = 0.4
    ac_kw: float = 3.5
    min_fan_bins: int = 16  # 4 hours of fan-only running before a room's fan effect is fitted
    # Where the thermostat really switches, relative to the setpoint, on the indoor reading used here.
    min_control_events: int = 6
    control_clamp: tuple = (-5.0, 2.0)
    max_control_spread: float = 2.0  # interquartile spread of the stop points above this means "too noisy"
    min_band_width: float = 0.25
    # Residual layer: what the one-node model misses under certain conditions (heat soak, weak AC).
    min_resid_days: int = 5
    resid_holdout_days: int = 2
    resid_min_gain: float = 0.05  # held-out replay error must drop at least this much to use it
    resid_clamp: float = 0.6  # a correction never exceeds this share of the AC's cooling rate
    # Free-form search: refine the best grid schedules one change at a time.
    max_nodes: int = 6
    min_gap: int = 60  # minutes between setpoint changes
    max_evals: int = 2000
    change_threshold: float = 0.01  # a move must save at least this much energy index to be kept
    change_penalty: float = 0.01  # per degree of setpoint swing, so the schedule does not zigzag
    stability_margin: float = 0.015  # keep the previous proposal unless the new one is this much better
    # Comfort learned from manual changes: tighten only, by at most learn_max_drop.
    learn_max_drop: float = 3.0
    learn_max_swing: float = 5.0  # bigger jumps look like testing, not comfort
    humid_comfort: float = 60.0

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
            control_clamp=(-2.8, 1.1),
            max_control_spread=1.1,
            min_band_width=0.15,
            learn_max_drop=1.5,
            learn_max_swing=2.8,
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
    fan: list = field(default_factory=list)  # [(epoch, 1.0 blower forced on / 0.0 not / None unknown)]
    fan_blocked: str | None = None  # why fan windows are off for this run (humidity, ...)
    thermostat_temp: list = field(default_factory=list)  # thermostat's own reading when `indoor` is another sensor
    past_plan: dict = field(default_factory=dict)  # {bin epoch: {"in", "rooms": {id: temp}}} from earlier planning runs
    previous_nodes: list | None = None  # last proposal, kept unless the new one is clearly better
    comfort_events: list = field(default_factory=list)  # manual setpoint changes with a snapshot of conditions
    comfort_forgotten: dict = field(default_factory=dict)  # {rule id: epoch} rules the user cleared
    humidity_now: float | None = None
    quick: bool = False  # a frequent refresh: smaller grid, the refinement does the rest


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


def fan_at(nodes: list, minute: int) -> bool:
    """Whether the node active at a local minute-of-day asks for the fan to circulate."""
    ordered = sorted(nodes, key=lambda n: to_minutes(n["time"]))
    mins = [to_minutes(n["time"]) for n in ordered]
    return bool(ordered[bisect_right(mins, minute) - 1].get("fan"))


def fan_plan(nodes: list) -> list[tuple[int, bool]]:
    """[{"time","temp","fan"?}] -> sorted [(minute_of_day, fan)]."""
    return sorted((to_minutes(n["time"]), bool(n.get("fan"))) for n in nodes)


def _step_at(plan: list, minute: int):
    mins = [p[0] for p in plan]
    return plan[bisect_right(mins, minute) - 1][1]


def merge_plan(nodes: list, fan: list | None) -> list[tuple[int, float, bool]]:
    """Combine setpoint nodes and fan steps into one list of (minute, temp, fan), dropping repeats."""
    if not fan:
        return [(m, t, False) for m, t in nodes]
    times = sorted({m for m, _ in nodes} | {m for m, _ in fan})
    out = []
    for m in times:
        step = (m, _step_at(nodes, m), bool(_step_at(fan, m)))
        if out and out[-1][1:] == step[1:]:
            continue
        out.append(step)
    if len(out) > 1 and out[0][1:] == out[-1][1:]:
        out.pop(0)  # the overnight node already carries the first state
    return out


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


def _band_of(band) -> tuple[float, float]:
    """(off_at, on_at) relative to the setpoint. A plain number is the old symmetric hysteresis."""
    if isinstance(band, (int, float)):
        return -float(band), float(band)
    return float(band[0]), float(band[1])


def model_band(m: dict, s: Settings) -> tuple[float, float]:
    return tuple(m["band"]) if m.get("band") else (-s.hysteresis, s.hysteresis)


def simulate(m: dict, To: list, S: list, ti0: float, sp: list, band, on0: int = 0, corr: tuple | None = None):
    """Thermostat plus house simulation on plain lists.

    band: (off_at, on_at), the AC stops below sp + off_at and starts above sp + on_at.
    corr: optional (w, u) per-bin rate corrections, w always and u while the AC runs.
    """
    k, g, q, c = m["k"], m["g"], m["q"], m["c"]
    off_at, on_at = _band_of(band)
    w, u = corr if corr else (None, None)
    Ti = [ti0]
    on = []
    state = on0
    t = ti0
    for i in range(len(sp)):
        if state:
            if t < sp[i] + off_at:
                state = 0
        elif t > sp[i] + on_at:
            state = 1
        on.append(state)
        rate = k * (To[i] - t) + g * S[i] / 1000.0 + q - c * state
        if w is not None:
            rate += w[i] + (u[i] if state else 0.0)
        t = t + STEP_H * rate
        Ti.append(t)
    return Ti, on


def _quantile(v: list, q: float) -> float:
    v = sorted(v)
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def learn_control(indoor: list, on: list, setpoint: list, s: Settings) -> dict:
    """Where the thermostat really starts and stops the AC, as offsets from the setpoint.

    Read at the raw on/off switching moments (bins would blur them). Starts right after a
    setpoint change are skipped (a drop forces a start at any temperature), and so are
    switches with no indoor reading close by and runs shorter than 5 minutes.
    """
    ipts = [(t, v) for t, v in indoor if v is not None]
    spts = []  # only the moments the setpoint value changed (states repeat it on every update)
    for t, v in sorted(setpoint, key=lambda p: p[0]):
        if v is not None and (not spts or abs(spts[-1][1] - v) > 1e-6):
            spts.append((t, v))
    out = {"off_at": None, "on_at": None, "starts": 0, "stops": 0, "spread": None, "source": "default"}
    if len(ipts) < 2 or not spts:
        return out
    its = [p[0] for p in ipts]
    sts = [p[0] for p in spts]

    def reading(ts):
        i = bisect_right(its, ts)
        if i == 0 or i >= len(its):
            return None
        (t0, v0), (t1, v1) = ipts[i - 1], ipts[i]
        if ts - t0 > 1800 and t1 - ts > 1800:
            return None
        return v0 + (v1 - v0) * (ts - t0) / (t1 - t0) if t1 > t0 else v0

    def setp(ts):
        i = bisect_right(sts, ts)
        if i == 0:
            return None, None
        return spts[i - 1][1], spts[i - 1][0]

    sw = []  # (ts, new_state)
    last = None
    for ts, v in sorted(on, key=lambda p: p[0]):
        if v is None:
            last = None
            continue
        st = 1 if v > 0.5 else 0
        if last is not None and st != last:
            sw.append((ts, st))
        last = st
    starts, stops = [], []
    for idx, (ts, st) in enumerate(sw):
        prev_t = sw[idx - 1][0] if idx > 0 else None
        next_t = sw[idx + 1][0] if idx + 1 < len(sw) else None
        sp, sp_since = setp(ts)
        tin = reading(ts)
        if sp is None or tin is None:
            continue
        if st == 1:
            if ts - sp_since < 1800 or (next_t is not None and next_t - ts < 300):
                continue
            starts.append(tin - sp)
        else:
            if prev_t is None or ts - prev_t < 300:
                continue
            stops.append(tin - sp)
    out["starts"], out["stops"] = len(starts), len(stops)
    if len(starts) < s.min_control_events or len(stops) < s.min_control_events:
        return out
    spread = _quantile(stops, 0.75) - _quantile(stops, 0.25)
    out["spread"] = spread
    if spread > s.max_control_spread:
        return out
    lo, hi = s.control_clamp
    off_at = max(lo, min(hi, _quantile(stops, 0.5)))
    on_at = max(lo, min(hi, _quantile(starts, 0.5)))
    on_at = max(on_at, off_at + s.min_band_width)
    out.update({"off_at": off_at, "on_at": on_at, "source": "learned"})
    return out


def replay(m: dict, Ti: list, on: list, sp: list, To: list, S: list, a: int, b: int, s: Settings, corr=None) -> dict | None:
    """Simulate history bins a..b with the REAL setpoints. Runtime and indoor error vs what happened."""
    seg_sp, seg_To, seg_S, seg_on = sp[a:b], To[a:b], S[a:b], on[a:b]
    if Ti[a] is None or any(v is None for v in seg_sp) or any(v is None for v in seg_To) or any(v is None for v in seg_S):
        return None
    known = [v for v in seg_on if v is not None]
    if len(known) < 0.8 * (b - a):
        return None
    first = seg_on[0] if seg_on[0] is not None else 0.0
    c = (corr[0][a:b], corr[1][a:b]) if corr else None
    T, p = simulate(m, seg_To, seg_S, Ti[a], seg_sp, model_band(m, s), 1 if first > 0.5 else 0, c)
    err = [(T[i] - Ti[a + i]) ** 2 for i in range(1, b - a) if Ti[a + i] is not None]
    return {
        "pred": sum(p) * STEP_H,
        "actual": sum(known) * STEP_H * ((b - a) / len(known)),
        "sq": sum(err),
        "n": len(err),
    }


def validate(m: dict, Ti: list, on: list, sp: list, To: list, S: list, idx0: int, s: Settings, days: int = 7, corr=None) -> dict:
    """Replay recent days with the REAL setpoints and compare AC runtime."""
    chunk = 96
    pred = actual = sq = 0.0
    used = cnt = 0
    for d in range(days):
        a = idx0 - (days - d) * chunk
        if a < 0:
            continue
        r = replay(m, Ti, on, sp, To, S, a, a + chunk, s, corr)
        if r is None:
            continue
        pred += r["pred"]
        actual += r["actual"]
        sq += r["sq"]
        cnt += r["n"]
        used += 1
    if used == 0 or actual <= 0:
        return {"days": 0, "predicted_hours": None, "actual_hours": None, "ratio": None, "indoor_rmse": None}
    return {
        "days": used,
        "predicted_hours": pred / used,
        "actual_hours": actual / used,
        "ratio": pred / actual,
        "indoor_rmse": math.sqrt(sq / cnt) if cnt else None,
    }


# ------------------------------------------------------------------ residual layer
def _resid_features(i: int, Ti_i: float, To: list, S: list, t_cap: float, s: Settings) -> tuple[list, float]:
    """Weather-only features (w part) and the AC-only capacity feature (u part) for bin i."""
    scale = 10.0 if s.fahrenheit else 5.5
    lo = max(0, i - 16)
    past = [v for v in S[lo:i] if v is not None]
    soak = (sum(past) / len(past) if past else S[i]) / 1000.0 - S[i] / 1000.0  # heat stored from earlier sun
    hot_sun = S[i] / 1000.0 * max(0.0, To[i] - Ti_i) / scale
    cap = max(0.0, To[i] - t_cap) / scale
    return [soak, hot_sun], cap


def fit_residual(m: dict, Ti: list, To: list, S: list, on: list, sp: list, idx0: int, s: Settings) -> dict:
    """Learn the conditions the one-node model gets wrong, checked on held-out days.

    The rate the model misses is regressed on heat soak (earlier sunshine), strong sun on
    a hot day, and outdoor heat while the AC runs (an AC losing capacity). It depends only
    on weather and AC state, never on the setpoint, so the search cannot game it.
    """
    out = {"used": False, "reason": None, "coef": None, "t_cap": None, "rmse_with": None, "rmse_without": None, "rows": 0}
    days = idx0 // 96
    if days < s.min_resid_days:
        out["reason"] = f"Needs {s.min_resid_days} days of history"
        return out
    k, g, q, c = m["k"], m["g"], m["q"], m["c"]
    hot = [To[i] for i in range(idx0) if on[i] is not None and on[i] > 0.5 and To[i] is not None]
    if len(hot) < 50:
        out["reason"] = "Not enough AC running in history"
        return out
    t_cap = _quantile(hot, 0.5)
    out["t_cap"] = t_cap
    hold = idx0 - s.resid_holdout_days * 96

    def rows_until(end):
        rows = []
        for i in range(end - 1):
            a, b, o, t, sun = Ti[i], Ti[i + 1], on[i], To[i], S[i]
            if a is None or b is None or o is None or t is None or sun is None:
                continue
            y = (b - a) / STEP_H
            if abs(y) > s.rate_limit:
                continue
            resid = y - (k * (t - a) + g * sun / 1000.0 + q - c * o)
            wf, cap = _resid_features(i, a, To, S, t_cap, s)
            wt = 0.5 ** ((idx0 - i) / (7 * 96))
            rows.append((wf + [o * cap], resid, wt))
        return rows

    def solve(rows):
        nf = 3
        XtX = [[0.0] * nf for _ in range(nf)]
        Xty = [0.0] * nf
        wsum = 0.0
        for x, y, wt in rows:
            wsum += wt
            for i in range(nf):
                Xty[i] += wt * x[i] * y
                for j in range(nf):
                    XtX[i][j] += wt * x[i] * x[j]
        for i in range(nf):
            XtX[i][i] += 0.05 * wsum
        beta = _solve(XtX, Xty)
        beta[2] = max(0.0, beta[2])  # heat can only make the AC weaker, never stronger
        return beta

    train = rows_until(hold)
    out["rows"] = len(train)
    if len(train) < 192:
        out["reason"] = "Not enough clean history rows"
        return out
    try:
        beta = solve(train)
    except ModelError:
        out["reason"] = "History does not vary enough"
        return out

    last_known = next((Ti[i] for i in range(idx0 - 1, -1, -1) if Ti[i] is not None), None)

    def corr_for(beta_, n):
        lim = s.resid_clamp * abs(c)
        w, u = [], []
        for i in range(n):
            if To[i] is None or S[i] is None:
                w.append(0.0)
                u.append(0.0)
                continue
            ref = Ti[i] if i < len(Ti) and Ti[i] is not None else (last_known if last_known is not None else To[i])
            wf, cap = _resid_features(i, ref, To, S, t_cap, s)
            w.append(max(-lim, min(lim, beta_[0] * wf[0] + beta_[1] * wf[1])))
            u.append(max(0.0, min(lim, beta_[2] * cap)))
        return w, u

    corr = corr_for(beta, len(To))
    sq_w = sq_wo = 0.0
    n_w = 0
    for a in range(hold, idx0, 96):
        r0 = replay(m, Ti, on, sp, To, S, a, min(a + 96, idx0), s)
        r1 = replay(m, Ti, on, sp, To, S, a, min(a + 96, idx0), s, corr)
        if r0 is None or r1 is None:
            continue
        sq_wo += r0["sq"]
        sq_w += r1["sq"]
        n_w += r0["n"]
    if n_w == 0:
        out["reason"] = "No clean held-out day to check against"
        return out
    out["rmse_without"] = math.sqrt(sq_wo / n_w)
    out["rmse_with"] = math.sqrt(sq_w / n_w)
    if out["rmse_with"] > (1 - s.resid_min_gain) * out["rmse_without"]:
        out["reason"] = "Did not beat the plain model on the last days"
        return out
    beta = solve(rows_until(idx0))  # refit on everything once it has proven itself
    out.update({"used": True, "coef": {"soak": beta[0], "hot_sun": beta[1], "capacity": beta[2]}})
    out["corr"] = corr_for(beta, len(To))
    return out


# ------------------------------------------------------------------ rooms and outdoor sensors
ROOM_WHEN = ("always", "awake", "asleep", "never")


def _duty(on: list, i: int) -> float | None:
    """Duty of a 0/1 series over the hour before Ti index i (bins i-4 .. i-1)."""
    vals = [v for v in on[max(0, i - 4) : i] if v is not None]
    return sum(vals) / len(vals) if vals else None


def fan_only(fan: list, on: list) -> list:
    """Blower running without the AC, per bin."""
    return [None if f is None or o is None else f * (1.0 - o) for f, o in zip(fan, on)]


def fit_room(R: list, Ti: list, To: list, S: list, on: list, s: Settings, fo: list | None = None) -> dict:
    """Least squares fit of the room offset against the thermostat reading."""
    fan_bins = sum(1 for v in fo or [] if v is not None and v > 0.5)
    use_fan = fan_bins >= s.min_fan_bins
    nf = 5 if use_fan else 4
    rows = []
    for i in range(len(R)):
        r, t, o, sun = R[i], Ti[i], To[i], S[i]
        d = _duty(on, i) if i > 0 else on[0]
        if r is None or t is None or o is None or sun is None or d is None:
            continue
        x = [1.0, o - t, sun / 1000.0, d]
        if use_fan:
            f = _duty(fo, i) if i > 0 else fo[0]
            if f is None:
                continue
            x.append(f)
        rows.append((x, r - t))
    out = {"rows": len(rows), "coef": None, "rmse": None, "r2": None, "trusted": False, "fan_learned": False}
    if len(rows) < s.min_room_rows:
        out["reason"] = f"Learning: {len(rows)} of {s.min_room_rows} readings"
        return out
    lam = 1e-3 * len(rows)  # light ridge so a feature that never moves (no sun at night) cannot blow up
    XtX = [[0.0] * nf for _ in range(nf)]
    Xty = [0.0] * nf
    for x, y in rows:
        for i in range(nf):
            Xty[i] += x[i] * y
            for j in range(nf):
                XtX[i][j] += x[i] * x[j]
    for i in range(1, nf):
        XtX[i][i] += lam
    try:
        beta = _solve(XtX, Xty)
    except ModelError:
        out["reason"] = "Readings do not vary enough to learn this room yet"
        return out
    ybar = sum(y for _, y in rows) / len(rows)
    ss_res = sum((y - sum(b * v for b, v in zip(beta, x))) ** 2 for x, y in rows)
    ss_tot = sum((y - ybar) ** 2 for _, y in rows)
    out["coef"] = beta + [0.0] * (5 - nf)
    out["fan_learned"] = use_fan
    out["rmse"] = math.sqrt(ss_res / len(rows))
    out["r2"] = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    out["trusted"] = out["rmse"] <= s.room_rmse_max
    out["reason"] = None if out["trusted"] else "Room tracks the thermostat too loosely to plan around"
    return out


def room_trace(coef: list, Ti: list, on: list, To: list, S: list, on0: int, fo: list | None = None, fo0: float = 0.0) -> list:
    """Predicted room temperature for each Ti index (Ti has one more element than on/To/S)."""
    a, b, c, d, e = coef
    n = len(To)
    out = []
    for i, t in enumerate(Ti):
        j = min(i, n - 1)
        duty = _duty(on, i) if i > 0 else float(on0)
        fd = (_duty(fo, i) if i > 0 else fo0) if fo else 0.0
        out.append(t + a + b * (To[j] - t) + c * S[j] / 1000.0 + d * duty + e * (fd or 0.0))
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
def _grid_temps(s: Settings, shift: float = 0.0) -> tuple[list, list]:
    """Day and night setpoints the grid tries. shift raises them when the house runs below its setpoint."""
    st = s.step
    day_top = _snap(s.awake_max + shift, st)
    night_top = math.floor((s.sleep_max + shift) / st + 1e-9) * st
    day_t = [_snap(day_top - st * i, st) for i in range(4, -1, -1)]
    night_t = [_snap(night_top - st * i, st) for i in range(5, -1, -1)]
    return day_t, night_t


def candidates(s: Settings, shift: float = 0.0, quick: bool = False):
    """quick: only the configured wake and sleep hours (the refinement moves them), for the frequent refreshes."""
    st = s.step
    day_t, night_t = _grid_temps(s, shift)
    spread = (0,) if quick else (-1, 0, 1)
    wakes = sorted({s.awake_start + d for d in spread if 0 <= s.awake_start + d <= 23})
    sleeps = sorted({s.awake_end + d for d in spread if 0 <= s.awake_end + d <= 23})
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


def _in_window(minute: int, start: int, end: int) -> bool:
    if start == end:
        return True  # a full day
    return start <= minute < end if start < end else minute >= start or minute < end


def period_of(minute: int, s: Settings) -> str:
    h = minute // 60
    if h >= s.sleep_start or h < s.sleep_end:
        return "asleep"
    if s.awake_start <= h < s.awake_end:
        return "awake"
    return "other"


def period_limits(local_minutes: list, s: Settings, when: str = "always") -> list:
    """Configured comfort limit for each bin (None outside the hours that apply)."""
    out = []
    for lm in local_minutes:
        p = period_of(lm, s)
        if p == "awake" and when in ("always", "awake"):
            out.append(s.awake_max)
        elif p == "asleep" and when in ("always", "asleep"):
            out.append(s.sleep_max)
        else:
            out.append(None)
    return out


def _tighten(lim: list, rule: dict, local_minutes: list, To: list, humidity: float | None) -> None:
    cond = rule.get("cond") or {}
    for j, lm in enumerate(local_minutes):
        if not _in_window(lm, rule["start"], rule["end"]):
            continue
        if "outdoor_min" in cond and (To[j] is None or To[j] < cond["outdoor_min"]):
            continue
        if "humidity_min" in cond and (humidity is None or humidity < cond["humidity_min"]):
            continue
        lim[j] = rule["max"] if lim[j] is None else min(lim[j], rule["max"])


def make_ctx(
    To_h: list,
    S_h: list,
    local_minutes: list,
    s: Settings,
    rooms: list | None = None,
    learned: list | None = None,
    humidity: float | None = None,
) -> dict:
    """rooms: [{"id", "coef", "when"}] for the trusted rooms that limit the search.

    learned: comfort rules that tighten a limit, {"kind": "house"|"room", "room", "start", "end", "max", "cond"}.
    Limits are per bin and checked at the bin's end (Ti index j + 1).
    """
    house = period_limits(local_minutes, s)
    ctx_rooms = []
    for room in rooms or []:
        lim = period_limits(local_minutes, s, room["when"])
        for rule in learned or []:
            if rule["kind"] == "room" and rule.get("room") == room.get("id"):
                _tighten(lim, rule, local_minutes, To_h, humidity)
        ctx_rooms.append({**room, "lim": [None] + lim})
    for rule in learned or []:
        if rule["kind"] == "house":
            _tighten(house, rule, local_minutes, To_h, humidity)
    return {"To": To_h, "S": S_h, "lm": local_minutes, "lim": [None] + house, "rooms": ctx_rooms}


def _over(T: list, lim: list) -> float:
    return max((T[i] - v for i, v in enumerate(lim) if v is not None), default=-99.0)


def evaluate(
    m: dict, nodes: list, ctx: dict, ti0: float, on0: int, s: Settings, keep: bool = False, fan: list | None = None
) -> dict:
    """fan: [(minute, bool)] steps, the blower circulates while the active step is True."""
    mins = [x[0] for x in nodes]
    temps = [x[1] for x in nodes]
    sp = [temps[bisect_right(mins, x) - 1] for x in ctx["lm"]]  # index -1 wraps to last node (overnight)
    Ti, on = simulate(m, ctx["To"], ctx["S"], ti0, sp, model_band(m, s), on0, ctx.get("corr"))
    energy = sum(
        o * STEP_H * max(0.5, 1 + s.energy_temp_coef * (t - s.ref_outdoor)) for o, t in zip(on, ctx["To"])
    )
    fan_bins = [1.0 if _step_at(fan, x) else 0.0 for x in ctx["lm"]] if fan else None
    fo = fan_only(fan_bins, on) if fan_bins else None
    if fo:
        energy += sum(fo) * STEP_H * s.fan_kw / s.ac_kw
    house = max(0.0, _over(Ti, ctx["lim"]))
    viol = house
    for room in ctx["rooms"]:
        Tr = room_trace(room["coef"], Ti, on, ctx["To"], ctx["S"], on0, fo, ctx.get("fo0", 0.0))
        viol = max(viol, _over(Tr, room["lim"]))
    swing = sum(abs(temps[i] - temps[i - 1]) for i in range(len(temps))) if len(temps) > 1 else 0.0
    out = {
        "nodes": nodes,
        "fan": fan,
        "energy": energy,
        "hours": sum(on) * STEP_H,
        "fan_hours": sum(fo) * STEP_H if fo else 0.0,
        "tmin": min(Ti),
        "tmax": max(Ti),
        "viol": viol,
        "viol_house": house,
        "score": energy + s.simplicity_penalty * (len(merge_plan(nodes, fan)) - 2) + s.change_penalty * swing,
    }
    if keep:
        out["Ti"] = Ti[:-1]  # one value per forecast bin
        out["on"] = on
        out["Tfull"] = Ti
        out["fo"] = fo
        out["fan_bins"] = fan_bins
    return out


def _better(a: dict, b: dict | None, s: Settings, margin: float = 0.0) -> bool:
    """a beats b: less comfort overshoot first, then lower score by more than margin."""
    if b is None:
        return True
    if a["viol"] < b["viol"] - 1e-4:
        return True
    return abs(a["viol"] - b["viol"]) <= 1e-4 and a["score"] < b["score"] - margin


def _valid_nodes(nodes: list, s: Settings, tlo: float, thi: float) -> bool:
    if not nodes or len(nodes) > s.max_nodes:
        return False
    mins = [m for m, _ in nodes]
    if len(set(mins)) != len(mins) or any(not 0 <= m < 1440 for m in mins):
        return False
    if any(t < tlo - 1e-6 or t > thi + 1e-6 for _, t in nodes):
        return False
    if len(nodes) > 1:
        gaps = [mins[i + 1] - mins[i] for i in range(len(mins) - 1)] + [1440 - mins[-1] + mins[0]]
        if min(gaps) < s.min_gap:
            return False
    return True


def _neighbors(nodes: list, s: Settings, tlo: float, thi: float):
    """Moves in a fixed order: shift a change an hour, nudge a temperature, drop a change, add one."""
    st = s.step
    for i, (mn, t) in enumerate(nodes):
        for d in (-60, 60):
            yield sorted(nodes[:i] + [((mn + d) % 1440, t)] + nodes[i + 1 :])
    for i, (mn, t) in enumerate(nodes):
        for d in (-st, st, -2 * st, 2 * st):
            yield nodes[:i] + [(mn, _snap(t + d, st))] + nodes[i + 1 :]
    if len(nodes) > 1:
        for i in range(len(nodes)):
            yield nodes[:i] + nodes[i + 1 :]
    if len(nodes) < s.max_nodes:
        mins = [m for m, _ in nodes]
        temps = [t for _, t in nodes]
        for h in range(24):
            mn = h * 60
            if mn in mins:
                continue
            cur = temps[bisect_right(mins, mn) - 1]
            for d in (-st, st):
                yield sorted(nodes + [(mn, _snap(cur + d, st))])


def refine(m: dict, seeds: list, ctx: dict, ti0: float, on0: int, s: Settings, tlo: float, thi: float) -> tuple[dict | None, int]:
    """Hill climb from each seed (nodes, fan) by single moves. Deterministic, capped at s.max_evals."""
    evals = 0
    best = None
    seen = set()
    for nodes, fan in seeds:
        nodes = sorted(nodes)
        if not _valid_nodes(nodes, s, tlo, thi) or (tuple(nodes), tuple(fan or ())) in seen:
            continue
        seen.add((tuple(nodes), tuple(fan or ())))
        cur = evaluate(m, nodes, ctx, ti0, on0, s, fan=fan)
        evals += 1
        while evals < s.max_evals:
            step_best = None
            for cand in _neighbors(cur["nodes"], s, tlo, thi):
                key = (tuple(cand), tuple(fan or ()))
                if key in seen or not _valid_nodes(cand, s, tlo, thi):
                    continue
                seen.add(key)
                r = evaluate(m, cand, ctx, ti0, on0, s, fan=fan)
                evals += 1
                if step_best is None or _better(r, step_best, s) or (
                    abs(r["viol"] - step_best["viol"]) <= 1e-4 and r["score"] == step_best["score"] and cand < step_best["nodes"]
                ):
                    step_best = r
                if evals >= s.max_evals:
                    break
            if step_best is None or not _better(step_best, cur, s, s.change_threshold):
                break
            cur = step_best
        if _better(cur, best, s):
            best = cur
    return best, evals


# ------------------------------------------------------------------ comfort learned from manual changes
def _local_day(ev: dict) -> int:
    return int((ev["ts"] - ev["lm"] * 60) // 86400)


def classify_change(ev: dict, s: Settings, hot_threshold: float | None, trusted_rooms: set) -> dict:
    """Why a turn-down most likely happened, from the snapshot taken at that moment."""
    gap = 1.0 if s.fahrenheit else 0.5
    indoor = ev.get("indoor")
    best_room, best_diff = None, 0.0
    for rid, rv in (ev.get("rooms") or {}).items():
        if rid not in trusted_rooms or rv.get("t") is None or indoor is None:
            continue
        diff = rv["t"] - indoor
        if diff >= gap and (rv.get("trend") or 0.0) >= 0 and diff > best_diff:
            best_room, best_diff = rid, diff
    period = period_of(ev["lm"], s)
    if best_room is not None:
        return {"cause": "room", "room": best_room, "period": period, "value": ev["rooms"][best_room]["t"] - s.step}
    base = (indoor if indoor is not None else ev["to"]) - s.step
    hum = ev.get("humidity")
    if hum is not None and hum >= s.humid_comfort:
        return {"cause": "humid", "room": None, "period": period, "value": base, "humidity": hum}
    out_t = ev.get("outdoor")
    if hot_threshold is not None and out_t is not None and out_t >= hot_threshold:
        return {"cause": "hot", "room": None, "period": period, "value": base, "outdoor": out_t}
    return {"cause": "time", "room": None, "period": period, "value": base}


def learn_comfort(events: list, now: float, s: Settings, hot_threshold: float | None, trusted_rooms: set, forgotten: dict | None = None) -> dict:
    """Comfort rules from repeated turn-downs. They only tighten the configured limits.

    A rule needs turn-downs for the same reason on 2 days within a week (or 3 within two
    weeks). Each change counts half as much every 14 days, and a turn-up in the same
    period counts against it, so a rule fades unless it keeps being confirmed.
    """
    forgotten = forgotten or {}
    groups: dict = {}
    ups: dict = {}
    for ev in events:
        age = (now - ev["ts"]) / 86400.0
        if age < 0 or age > 28 or abs(ev["to"] - ev["from"]) > s.learn_max_swing or ev.get("source") == "backfill":
            continue
        weight = 0.5 ** (age / 14.0)
        if ev["to"] > ev["from"]:
            p = period_of(ev["lm"], s)
            ups.setdefault(p, []).append((ev, weight))
            continue
        c = classify_change(ev, s, hot_threshold, trusted_rooms)
        rid = f"{c['cause']}:{c['room'] or ''}:{c['period']}"
        if ev["ts"] <= forgotten.get(rid, -1):
            continue
        groups.setdefault(rid, []).append((ev, c, weight))
    rules, pending = [], []
    for rid, items in sorted(groups.items()):
        cause, room, period = rid.split(":")
        days = sorted({_local_day(ev) for ev, _, _ in items})
        recent = [d for d in days if (now - ev_day_start(d)) / 86400.0 <= 14]
        confirmed = len(recent) >= 3 or any(b - a <= 7 for a, b in zip(recent, recent[1:]))
        strength = sum(w for _, _, w in items) - sum(w for _, w in ups.get(period, []))
        values = sorted(c["value"] for _, c, _ in items)
        value = values[len(values) // 2]
        limit = s.awake_max if period == "awake" else s.sleep_max if period == "asleep" else max(s.awake_max, s.sleep_max)
        value = max(value, limit - s.learn_max_drop)
        start_h = min(ev["lm"] // 60 for ev, _, _ in items) - 1
        end = (s.awake_end if period == "awake" else s.sleep_end if period == "asleep" else (start_h + 4) % 24) * 60
        rule = {
            "id": rid,
            "cause": cause,
            "kind": "room" if cause == "room" else "house",
            "room": room or None,
            "period": period,
            "start": (start_h % 24) * 60,
            "end": end,
            "max": _snap(value, s.step / 2),
            "changes": len(items),
            "days": len(days),
            "strength": round(strength, 2),
            "cond": None,
        }
        if cause == "hot":
            rule["cond"] = {"outdoor_min": round(min(c["outdoor"] for _, c, _ in items) - (2.0 if s.fahrenheit else 1.0), 1)}
        elif cause == "humid":
            rule["cond"] = {"humidity_min": round(min(c["humidity"] for _, c, _ in items) - 3.0, 1)}
        if rule["max"] >= limit:
            continue  # already inside the configured limit, nothing to tighten
        (rules if confirmed and strength >= 1.0 else pending).append(rule)
    notes = []
    for period, items in sorted(ups.items()):
        days = {_local_day(ev) for ev, _ in items}
        if len(days) >= 2:
            hrs = sorted(ev["lm"] // 60 for ev, _ in items)
            notes.append({"period": period, "days": len(days), "hour": hrs[len(hrs) // 2]})
    return {"rules": rules, "pending": pending, "notes": notes}


def ev_day_start(day: int) -> float:
    return day * 86400.0


def fan_options(s: Settings) -> list:
    """Circulate windows to try, as [(minute, bool)] steps."""
    def win(a, b):
        return [((a % 24) * 60, True), ((b % 24) * 60, False)]

    # Windows open an hour early: a room feels the fan over the following hour, not at once.
    opts = [
        win(s.sleep_start - 1, s.sleep_end),
        win(s.sleep_start - 1, s.sleep_start + 2),
        win(s.sleep_start - 2, s.sleep_start),
        win(s.awake_end - 3, s.awake_end),
        win(s.awake_start - 1, s.awake_end),
    ]
    seen, out = set(), []
    for o in opts:
        if o[0][0] != o[1][0] and tuple(o) not in seen:
            seen.add(tuple(o))
            out.append(sorted(o))
    return out


def stepSeries_nodes(nodes: list, local_minutes: list) -> list:
    mins = [x[0] for x in nodes]
    temps = [x[1] for x in nodes]
    return [temps[bisect_right(mins, x) - 1] for x in local_minutes]


def accuracy(actual: list, planned: list, min_pairs: int = 24) -> dict | None:
    """Mean absolute error and bias of planned against actual, None with too few pairs."""
    pairs = [(a, p) for a, p in zip(actual, planned) if a is not None and p is not None]
    if len(pairs) < min_pairs:
        return None
    return {
        "mae": round(sum(abs(a - p) for a, p in pairs) / len(pairs), 2),
        "bias": round(sum(a - p for a, p in pairs) / len(pairs), 2),
        "n": len(pairs),
    }


def _pack(r: dict | None) -> dict | None:
    if r is None:
        return None
    return {
        "nodes": [
            {"time": hhmm(mn), "temp": _fmt_temp(t), **({"fan": True} if f else {})}
            for mn, t, f in merge_plan(r["nodes"], r.get("fan"))
        ],
        "ac_hours": round(r["hours"], 2),
        "fan_hours": round(r.get("fan_hours", 0.0), 2),
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
    val_default = validate(m, Ti, on, sp, To, S, idx0, s)
    val = val_default

    # Where the thermostat really switches. Kept only if it replays recent runtime better.
    control = learn_control(inp.indoor, inp.on, inp.setpoint, s)
    control["ratio_default"] = val_default["ratio"]
    control["ratio_learned"] = None
    if control["source"] == "learned":
        m_l = {**m, "band": [control["off_at"], control["on_at"]]}
        val_l = validate(m_l, Ti, on, sp, To, S, idx0, s)
        control["ratio_learned"] = val_l["ratio"]
        if val_l["ratio"] is not None and (
            val_default["ratio"] is None or abs(val_l["ratio"] - 1) < abs(val_default["ratio"] - 1)
        ):
            m, val = m_l, val_l
        else:
            control["source"] = "rejected"
    band = model_band(m, s)

    # What the model misses under certain conditions, used only if it beats the plain model on held-out days.
    resid = fit_residual(m, Ti, To, S, on, sp, idx0, s)
    corr_full = resid.pop("corr", None)
    if corr_full is not None:
        val_c = validate(m, Ti, on, sp, To, S, idx0, s, corr=corr_full)
        resid["ratio"] = val_c["ratio"]
        if val_c["ratio"] is not None:
            val = val_c

    known = [i for i in range(idx0) if Ti[i] is not None]
    if not known:
        raise ModelError("No recent indoor temperature readings in the recorder")
    ti0 = Ti[known[-1]]
    onk = [i for i in range(idx0) if on[i] is not None]
    on0 = 1 if onk and on[onk[-1]] > 0.5 else 0

    fan_hist = resample(inp.fan, inp.g0, inp.n_hist, inp.data_end) if inp.fan else [None] * idx0
    fo_hist = fan_only(fan_hist, on[:idx0])
    fok = [v for v in fo_hist if v is not None]
    fo0 = fok[-1] if fok else 0.0

    rooms = []
    for room in inp.rooms:
        R = resample(room["points"], inp.g0, inp.n_hist, inp.data_end)
        fit = fit_room(R, Ti[:idx0], To[:idx0], S[:idx0], on[:idx0], s, fo_hist)
        rk = [i for i in range(idx0) if R[i] is not None]
        rooms.append({**{k: room.get(k) for k in ("id", "name", "when")}, **fit, "R": R, "now": R[rk[-1]] if rk else None})
    limiting = [
        {"id": r["id"], "coef": r["coef"], "when": r["when"], "fan": r["fan_learned"]}
        for r in rooms
        if r["trusted"] and r["when"] != "never"
    ]

    # Comfort rules from manual turn-downs, read against the whole picture at each change.
    daily_max = {}
    for i in range(idx0):
        if To[i] is not None:
            d = (ts[i] - lm_all[i] * 60) // 86400
            daily_max[d] = max(daily_max.get(d, -999.0), To[i])
    hot_threshold = _quantile(list(daily_max.values()), 0.8) if len(daily_max) >= 3 else None
    comfort = learn_comfort(
        inp.comfort_events, inp.data_end, s, hot_threshold, {r["id"] for r in limiting}, inp.comfort_forgotten
    )

    ctx = make_ctx(To[idx0:], S[idx0:], inp.local_minutes, s, limiting, comfort["rules"], inp.humidity_now)
    ctx["fo0"] = fo0
    if corr_full is not None:
        ctx["corr"] = (corr_full[0][idx0:], corr_full[1][idx0:])
    base_nodes = sorted((int(a), float(b)) for a, b in s.baseline)
    base = evaluate(m, base_nodes, ctx, ti0, on0, s, keep=True)
    active = None
    if inp.active_nodes:
        plan = fan_plan(inp.active_nodes)
        active = evaluate(
            m, parse_nodes(inp.active_nodes), ctx, ti0, on0, s, keep=True, fan=plan if any(f for _, f in plan) else None
        )

    # The fan only ever helps a room, so it is tried just for schedules the house passes and a room fails.
    use_fan = s.fan_enabled and not inp.fan_blocked and any(r["fan"] for r in limiting)
    fans = fan_options(s) if use_fan else []
    # A house that holds below its setpoint needs higher setpoint numbers for the same comfort.
    shift = math.floor(max(0.0, -(band[0] + band[1]) / 2) / s.step + 1e-9) * s.step
    best = least_bad = None
    top: list = []  # best few comfortable grid schedules, seeds for the free-form refinement
    n_cand = n_ok = 0
    for nodes in candidates(s, shift, inp.quick):
        tries = [evaluate(m, nodes, ctx, ti0, on0, s)]
        if fans and tries[0]["viol"] > 0 and tries[0]["viol_house"] <= 0:
            tries += [evaluate(m, nodes, ctx, ti0, on0, s, fan=f) for f in fans]
        for r in tries:
            n_cand += 1
            if least_bad is None or (r["viol"], r["score"]) < (least_bad["viol"], least_bad["score"]):
                least_bad = r
            if r["viol"] <= 0.0:
                n_ok += 1
                if best is None or r["score"] < best["score"]:
                    best = r
                top.append(r)
                top.sort(key=lambda x: x["score"])
                del top[3:]

    # Free-form refinement: move, nudge, drop or add one change at a time from the best seeds.
    day_t, night_t = _grid_temps(s, shift)
    tlo, thi = min(day_t + night_t) - 2 * s.step, max(day_t + night_t) + s.step
    seeds = [(r["nodes"], r["fan"]) for r in top] or ([(least_bad["nodes"], least_bad["fan"])] if least_bad else [])
    for extra in (inp.active_nodes, inp.previous_nodes):
        if extra:
            plan = fan_plan(extra)
            seeds.append((parse_nodes(extra), plan if any(f for _, f in plan) else None))
    refined, n_refine = refine(m, seeds, ctx, ti0, on0, s, tlo, thi)
    if refined is not None and fans and refined["viol"] > 0 and refined["viol_house"] <= 0:
        for f in fans:
            r = evaluate(m, refined["nodes"], ctx, ti0, on0, s, fan=f)
            if _better(r, refined, s):
                refined = r
    if refined is not None:
        if _better(refined, least_bad, s):
            least_bad = refined
        if refined["viol"] <= 0.0 and (best is None or refined["score"] < best["score"]):
            best = refined

    # Keep the previous proposal unless the new one is clearly better, so it does not flap between refreshes.
    kept_previous = False
    if best is not None and inp.previous_nodes:
        plan = fan_plan(inp.previous_nodes)
        prev = evaluate(m, parse_nodes(inp.previous_nodes), ctx, ti0, on0, s, fan=plan if any(f for _, f in plan) else None)
        if prev["viol"] <= 0.0 and prev["score"] <= best["score"] * (1 + s.stability_margin):
            best, kept_previous = prev, True

    if best is not None:
        best = evaluate(m, best["nodes"], ctx, ti0, on0, s, keep=True, fan=best["fan"])  # re-run to keep the traces

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

    # What earlier planning runs predicted for the past bins, next to what happened.
    plan_rows = [inp.past_plan.get(ts[i]) or {} for i in range(a, idx0)]
    indoor_planned = [r1(row.get("in")) for row in plan_rows]
    house_acc = accuracy(Ti[a:idx0], indoor_planned)

    manual = []
    for ev in inp.comfort_events:
        i = int((ev["ts"] - ts[a]) // STEP_S)
        if 0 <= i < past:
            manual.append({"i": i, "from": ev["from"], "to": ev["to"], "source": ev.get("source")})

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
        "fan_actual": [None if v is None else round(v, 2) for v in fan_hist[a:idx0]] + [None] * inp.n_hor,
        "fan": {k: [None] * past + [int(x) for x in (r["fan_bins"] or [0] * inp.n_hor)] for k, r in traces.items()},
        "limit": period_limits(lm_past, s) + [None if v is None else round(v, 2) for v in ctx["lim"][1:]],
        "indoor_planned": indoor_planned + [None] * inp.n_hor,
        "accuracy": house_acc,
        "manual": manual,
        "rooms": [],
    }
    for room in rooms:
        pred = {}
        if room["coef"] is not None:
            for k, r in traces.items():
                Tr = room_trace(room["coef"], r["Tfull"], r["on"], To[idx0:], S[idx0:], on0, r["fo"], fo0)[:-1]
                pred[k] = [None] * past + [r1(x) for x in Tr]
                r.setdefault("room_max", {})[room["id"]] = max(Tr)
        planned = [r1((row.get("rooms") or {}).get(room["id"])) for row in plan_rows]
        series["rooms"].append(
            {
                "id": room["id"],
                "name": room["name"],
                "when": room["when"],
                "trusted": room["trusted"],
                "actual": [r1(x) for x in room["R"][a:idx0]] + [None] * inp.n_hor,
                "predicted": pred,
                "planned": planned + [None] * inp.n_hor,
                "accuracy": accuracy(room["R"][a:idx0], planned),
            }
        )

    # The forecast this run plans on, for the integration to store against each future bin.
    plan_key = "active" if active is not None else "baseline"
    pt = traces[plan_key]
    sp_plan = stepSeries_nodes(pt["nodes"], inp.local_minutes)
    plan_bins = []
    for j in range(inp.n_hor):
        row = {
            "ts": ts[idx0 + j],
            "in": r1(pt["Ti"][j]),
            "sp": sp_plan[j],
            "ac": int(pt["on"][j]),
            "to": r1(To[idx0 + j]),
            "sun": round(S[idx0 + j]),
            "rooms": {},
        }
        for room in series["rooms"]:
            v = room["predicted"].get(plan_key)
            if v:
                row["rooms"][room["id"]] = v[past + j]
        plan_bins.append(row)

    indoor_source = None
    if inp.thermostat_temp:
        tt = resample(inp.thermostat_temp, inp.g0, inp.n_hist, inp.data_end)
        diffs = [Ti[i] - tt[i] for i in range(idx0) if Ti[i] is not None and tt[i] is not None]
        if len(diffs) >= 96:
            indoor_source = {"thermostat_gap": round(_quantile(diffs, 0.5), 2), "bins": len(diffs)}

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
                "coef": None if r["coef"] is None else dict(zip(("offset", "outdoor", "sun", "ac", "fan"), (round(c, 3) for c in r["coef"]))),
                "fan_learned": r["fan_learned"],
                "now": r1(r["now"]),
                "max_next_24h": {k: r1(t.get("room_max", {}).get(r["id"])) for k, t in traces.items()},
            }
            for r in rooms
        ],
        "outdoor_sensors": outdoor_info,
        "control": {
            k: (round(v, 2) if isinstance(v, float) else v) for k, v in control.items()
        } | {"used_off_at": round(band[0], 2), "used_on_at": round(band[1], 2), "setpoint_shift": shift},
        "indoor_source": indoor_source,
        "residual": {
            k: (round(v, 3) if isinstance(v, float) else ({kk: round(vv, 3) for kk, vv in v.items()} if isinstance(v, dict) else v))
            for k, v in resid.items()
        },
        "comfort_learned": comfort,
        "hot_threshold": r1(hot_threshold),
        "search": {"refine_evals": n_refine, "kept_previous": kept_previous, "max_nodes": s.max_nodes},
        "plan_bins": plan_bins,
        "plan_from": plan_key,
        "fan": {
            "enabled": s.fan_enabled,
            "blocked": inp.fan_blocked,
            "learned_rooms": sum(1 for r in limiting if r["fan"]),
            "searched": use_fan,
            "proposed_hours": round(best["fan_hours"], 2) if best is not None else None,
            "history_fan_only_hours": round(sum(fok) * STEP_H, 1),
        },
    }
