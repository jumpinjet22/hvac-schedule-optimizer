/*
 * HVAC Schedule Card
 * Ships with the HVAC Schedule Optimizer integration and loads itself, no resource to add.
 *
 *   type: custom:hvac-schedule-card
 *   climate_entity: climate.living_room_thermostat   # optional if you only have one
 *   title: Cooling schedule                           # optional
 */
const CARD_VERSION = "0.7.0-beta.1";
const DOMAIN = "hvac_schedule_optimizer";
const REFRESH_MS = 120000;
const SVG_NS = "http://www.w3.org/2000/svg";
const ROOM_COLORS = ["#ab47bc", "#26a69a", "#ef6c00", "#5c6bc0", "#ec407a", "#9e9d24", "#8d6e63", "#29b6f6"];
const HIDDEN_KEY = "hvac-schedule-card:hidden-rooms";
const ROOM_WHEN = { always: "always", awake: "awake hours", asleep: "sleep hours", never: "watch only" };

const STATUS = {
  ready: { label: "Ready", tone: "good" },
  needs_review: { label: "Needs review", tone: "warn" },
  no_comfortable_schedule: { label: "No comfortable schedule", tone: "bad" },
  error: { label: "Error", tone: "bad" },
};

const STYLE = `
:host { display: block; }
ha-card { overflow: hidden; --hso-accent: var(--primary-color, #03a9f4); --hso-good: var(--success-color, #43a047);
  --hso-warn: var(--warning-color, #f9a825); --hso-bad: var(--error-color, #e53935);
  --hso-cool: #4aa3ff; --hso-sun: #ffb74d; --hso-heat: #ff7043; --hso-muted: var(--secondary-text-color, #727272); }
.wrap { padding: 16px 16px 14px; position: relative; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.title { font-size: 1.15rem; font-weight: 500; color: var(--primary-text-color); line-height: 1.2; }
.sub { font-size: .8rem; color: var(--hso-muted); margin-top: 2px; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; font-size: .75rem;
  font-weight: 500; white-space: nowrap; background: color-mix(in srgb, var(--tone) 16%, transparent); color: var(--tone); }
.chip::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--tone); }
.chip.good { --tone: var(--hso-good); } .chip.warn { --tone: var(--hso-warn); } .chip.bad { --tone: var(--hso-bad); }
.chip.muted { --tone: var(--hso-muted); }
.stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 14px; }
.stat { background: var(--secondary-background-color, rgba(127,127,127,.08)); border-radius: 12px; padding: 10px 12px; min-width: 0; }
.stat .k { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--hso-muted); }
.stat .v { font-size: 1.5rem; font-weight: 500; color: var(--primary-text-color); line-height: 1.25; white-space: nowrap; }
.stat .v small { font-size: .8rem; font-weight: 400; color: var(--hso-muted); margin-left: 2px; }
.stat .n { font-size: .72rem; color: var(--hso-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.stat .v.good { color: var(--hso-good); } .stat .v.bad { color: var(--hso-bad); }
.chartbox { position: relative; }
svg { display: block; width: 100%; height: auto; touch-action: pan-y; user-select: none; }
svg text { font-family: var(--paper-font-body1_-_font-family, inherit); fill: var(--hso-muted); font-size: 10.5px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 8px 2px 0; font-size: .72rem; color: var(--hso-muted); }
.legend i { display: inline-block; width: 14px; height: 0; border-top: 2px solid; vertical-align: middle; margin-right: 5px; }
.legend .room { cursor: pointer; user-select: none; } .legend .room.off { opacity: .4; text-decoration: line-through; }
.legend i.dash { border-top-style: dashed; } .legend i.box { height: 8px; border: 0; border-radius: 2px; }
.tip { position: absolute; pointer-events: none; z-index: 2; min-width: 150px; padding: 8px 10px; border-radius: 10px; font-size: .74rem;
  background: var(--card-background-color, #fff); color: var(--primary-text-color); box-shadow: 0 4px 18px rgba(0,0,0,.28);
  border: 1px solid var(--divider-color, rgba(127,127,127,.3)); opacity: 0; transition: opacity .1s; line-height: 1.55; }
.tip.on { opacity: 1; } .tip b { font-weight: 600; } .tip .r { display: flex; justify-content: space-between; gap: 14px; }
.tip .r span:first-child { color: var(--hso-muted); }
.nodes { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 14px; }
.col h4 { margin: 0 0 6px; font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--hso-muted); font-weight: 500; }
.pill { display: inline-flex; gap: 6px; align-items: baseline; padding: 3px 9px; margin: 0 5px 5px 0; border-radius: 8px; font-size: .78rem;
  background: var(--secondary-background-color, rgba(127,127,127,.08)); color: var(--primary-text-color); }
.pill span { color: var(--hso-muted); font-size: .7rem; }
.col.prop .pill { background: color-mix(in srgb, var(--hso-accent) 14%, transparent); }
.rt { margin-top: 14px; }
.rt h4 { margin: 0 0 8px; font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--hso-muted); font-weight: 500; }
.rtgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 10px; }
.rtgrid div { font-size: .72rem; color: var(--hso-muted); }
.rtgrid b { display: block; font-size: 1.1rem; font-weight: 500; color: var(--primary-text-color); }
.rtgrid b small { font-size: .72rem; font-weight: 400; color: var(--hso-muted); }
.rtgrid b.acc { color: var(--hso-accent); }
.rtgrid b small.good { color: var(--hso-good); } .rtgrid b small.warn { color: var(--hso-warn); } .rtgrid b small.bad { color: var(--hso-bad); }
.bars { display: flex; align-items: flex-end; gap: 2px; height: 54px; }
.bars .h { flex: 1; display: flex; align-items: flex-end; gap: 1px; height: 100%; }
.bars .h i { flex: 1; border-radius: 2px 2px 0 0; min-height: 0; background: var(--hso-muted); opacity: .55; }
.bars .h i.p { background: var(--hso-accent); opacity: .9; }
.barlab { display: flex; justify-content: space-between; font-size: .66rem; color: var(--hso-muted); margin-top: 3px; }
.msg { margin-top: 12px; padding: 9px 12px; border-radius: 10px; font-size: .8rem; line-height: 1.4;
  background: color-mix(in srgb, var(--hso-warn) 14%, transparent); color: var(--primary-text-color); }
.msg.bad { background: color-mix(in srgb, var(--hso-bad) 14%, transparent); }
.msg.info { background: color-mix(in srgb, var(--hso-good) 12%, transparent); }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
button.b { font: inherit; font-size: .82rem; font-weight: 500; border: 0; border-radius: 999px; padding: 8px 16px; cursor: pointer;
  background: var(--secondary-background-color, rgba(127,127,127,.12)); color: var(--primary-text-color); transition: filter .15s, opacity .15s; }
button.b:hover:not(:disabled) { filter: brightness(1.12); }
button.b.primary { background: var(--hso-accent); color: var(--text-primary-color, #fff); }
button.b:disabled { opacity: .45; cursor: default; }
.toggles { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--divider-color, rgba(127,127,127,.2)); }
label.tg { display: inline-flex; align-items: center; gap: 9px; font-size: .82rem; color: var(--primary-text-color); cursor: pointer; }
.sw { position: relative; width: 34px; height: 20px; border-radius: 999px; background: var(--disabled-text-color, #9e9e9e); transition: background .15s; flex: none; }
.sw::after { content: ""; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: transform .15s; }
input:checked + .sw { background: var(--hso-accent); } input:checked + .sw::after { transform: translateX(14px); }
input[type=checkbox] { position: absolute; opacity: 0; pointer-events: none; }
.learn { margin-top: 14px; }
.learn h4 { margin: 0 0 6px; font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: var(--hso-muted); font-weight: 500; }
.rule { display: inline-flex; align-items: center; gap: 6px; padding: 3px 4px 3px 9px; margin: 0 5px 5px 0; border-radius: 8px; font-size: .76rem;
  background: color-mix(in srgb, var(--hso-warn) 16%, transparent); color: var(--primary-text-color); }
.rule.pending { background: var(--secondary-background-color, rgba(127,127,127,.08)); color: var(--hso-muted); padding-right: 9px; }
.rule span { color: var(--hso-muted); font-size: .7rem; }
.rule button { font: inherit; border: 0; background: transparent; color: var(--hso-muted); cursor: pointer; padding: 0 5px; font-size: .9rem; line-height: 1; }
.note { font-size: .74rem; color: var(--hso-muted); margin: 2px 0 4px; }
.linkbtn { font: inherit; font-size: .74rem; border: 0; background: transparent; color: var(--hso-accent); cursor: pointer; padding: 0; }
.model { margin-top: 10px; font-size: .74rem; color: var(--hso-muted); line-height: 1.5; }
.empty { padding: 30px 8px; text-align: center; color: var(--hso-muted); font-size: .9rem; }
.spin { opacity: .55; pointer-events: none; }
@media (max-width: 480px) { .stat .v { font-size: 1.25rem; } .nodes { grid-template-columns: 1fr; } }
`;

const el = (tag, attrs = {}, parent) => {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
};

const toMin = (s) => {
  const [h, m] = String(s).split(":");
  return Number(h) * 60 + Number(m);
};

function stepSeries(nodes, lm, key = "temp") {
  const have = (nodes || []).filter((x) => x[key] !== undefined && x[key] !== null);
  if (!have.length) return lm.map(() => null);
  const n = have.map((x) => [toMin(x.time), Number(x[key])]).sort((a, b) => a[0] - b[0]);
  return lm.map((m) => {
    let t = n[n.length - 1][1];
    for (const [nm, nt] of n) if (nm <= m) t = nt;
    return t;
  });
}

function fmtHour(m) {
  const h = Math.floor(m / 60) % 24;
  const suffix = h < 12 ? "AM" : "PM";
  return `${h % 12 === 0 ? 12 : h % 12} ${suffix}`;
}
function fmtClock(m) {
  const h = Math.floor(m / 60) % 24;
  const mm = String(Math.round(m % 60)).padStart(2, "0");
  return `${h % 12 === 0 ? 12 : h % 12}:${mm} ${h < 12 ? "AM" : "PM"}`;
}
const f1 = (v) => (v === null || v === undefined ? "-" : Number(v).toFixed(1).replace(/\.0$/, ""));

class HvacScheduleCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._state = null;
    this._error = null;
    this._busy = false;
    this._timer = null;
    this._loaded = false;
    this._hiddenPref = {};
    try {
      this._hiddenPref = JSON.parse(localStorage.getItem(HIDDEN_KEY) || "{}") || {};
    } catch (_) {
      this._hiddenPref = {};
    }
  }

  // Rooms set to "watch only" start hidden; a tap on the legend overrides that per device.
  _roomHidden(room) {
    return room.id in this._hiddenPref ? !!this._hiddenPref[room.id] : room.when === "never";
  }

  _toggleRoom(id, hidden) {
    this._hiddenPref = { ...this._hiddenPref, [id]: hidden };
    try {
      localStorage.setItem(HIDDEN_KEY, JSON.stringify(this._hiddenPref));
    } catch (_) {}
    this._render();
  }

  static getStubConfig(hass) {
    const c = Object.keys(hass.states).find((e) => e.startsWith("climate."));
    return { type: "custom:hvac-schedule-card", ...(c ? { climate_entity: c } : {}) };
  }

  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  getCardSize() {
    return 9;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this.isConnected) this._fetch();
  }

  connectedCallback() {
    if (this._hass && !this._loaded) this._fetch();
    this._timer = setInterval(() => this._fetch(true), REFRESH_MS);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
  }

  // ------------------------------------------------------------------ data
  _call(type, extra = {}) {
    const msg = { type: `${DOMAIN}/${type}`, ...extra };
    if (this._config && this._config.climate_entity) msg.climate_entity = this._config.climate_entity;
    return this._hass.callWS(msg);
  }

  async _fetch(quiet = false) {
    if (!this._hass || this._busy) return;
    try {
      this._state = await this._call("state");
      this._error = null;
    } catch (err) {
      if (!quiet || !this._state) this._error = (err && err.message) || "Could not reach the integration";
    }
    this._loaded = true;
    this._render();
  }

  async _act(type, extra) {
    if (this._busy) return;
    this._busy = true;
    this._notice = null;
    this._render();
    try {
      this._state = await this._call(type, extra);
      this._error = null;
    } catch (err) {
      this._notice = (err && err.message) || "That did not work";
    }
    this._busy = false;
    this._render();
  }

  // ------------------------------------------------------------------ render
  _render() {
    const root = this.shadowRoot;
    if (!root) return;
    const mode0 = this._state && this._state.result && this._state.result.mode;
    const title = (this._config && this._config.title) ||
      (mode0 === "heat" ? "Heating schedule" : mode0 === "heat_cool" ? "Heating and cooling" : "Cooling schedule");
    root.innerHTML = `<style>${STYLE}</style><ha-card><div class="wrap ${this._busy ? "spin" : ""}"></div></ha-card>`;
    const wrap = root.querySelector(".wrap");

    if (this._error && !this._state) {
      wrap.innerHTML = `<div class="head"><div class="title">${title}</div></div>
        <div class="msg bad">${this._esc(this._error)}</div>`;
      return;
    }
    if (!this._state) {
      wrap.innerHTML = `<div class="head"><div class="title">${title}</div></div><div class="empty">Loading...</div>`;
      return;
    }

    const { result: r, schedule: sch } = this._state;
    const st = STATUS[r.status] || { label: "Waiting", tone: "muted" };
    const unit = r.unit || this._hass.config.unit_system.temperature || "";
    const climate = this._hass.states[sch.climate_entity];
    const nowIndoor = climate ? climate.attributes.current_temperature : null;
    const change = r.energy_change_pct;
    const vsActive = r.change_vs_active_pct;
    const shown = vsActive !== null && vsActive !== undefined ? vsActive : change;
    const shownLabel = vsActive !== null && vsActive !== undefined ? "vs active" : "vs baseline";
    const when = sch.next_change ? new Date(sch.next_change) : null;
    const mode = r.mode || "cool";
    const nextNode = when ? (sch.nodes || []).find((x) => toMin(x.time) === when.getHours() * 60 + when.getMinutes()) : null;
    const spText = (cool, heat) =>
      mode === "heat" ? (heat === null || heat === undefined ? "-" : `heat ${f1(heat)}${unit}`)
      : mode === "heat_cool" && heat !== null && heat !== undefined ? `${f1(heat)}\u2013${f1(cool)}${unit}`
      : `${f1(cool)}${unit}`;
    const updated = r.generated_at ? new Date(r.generated_at) : null;

    wrap.innerHTML = `
      <div class="head">
        <div>
          <div class="title">${this._esc(title)}</div>
          <div class="sub">${sch.enabled ? "Running" : "Paused"} \u00b7 source: ${this._esc(sch.source)}${
            updated ? " \u00b7 updated " + updated.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : ""
          }</div>
        </div>
        <span class="chip ${st.tone}">${st.label}</span>
      </div>
      <div class="stats">
        <div class="stat"><div class="k">Indoor now</div><div class="v">${f1(nowIndoor)}<small>${unit}</small></div>
          <div class="n">target ${spText(sch.setpoint_now, sch.heat_now)}</div></div>
        <div class="stat"><div class="k">Next change</div><div class="v">${when ? fmtClock(when.getHours() * 60 + when.getMinutes()).replace(/ (AM|PM)/, "<small>$1</small>") : "-"}</div>
          <div class="n">${when ? "set to " + spText(sch.next_temp, nextNode ? nextNode.heat : null) : ""}</div></div>
        <div class="stat"><div class="k">Proposed</div>
          <div class="v ${shown === null || shown === undefined ? "" : shown <= 0 ? "good" : "bad"}">${
            shown === null || shown === undefined ? "-" : (shown > 0 ? "+" : "") + f1(shown) + "<small>%</small>"
          }</div><div class="n">${shown === null || shown === undefined ? "no proposal yet" : "energy " + shownLabel}</div></div>
      </div>
      <div class="chartbox"><div class="chart"></div><div class="tip"></div></div>
      <div class="legend"></div>
      <div class="rt"></div>
      <div class="model"></div>
      <div class="msgs"></div>
      <div class="nodes"></div>
      <div class="learn"></div>
      <div class="actions"></div>
      <div class="toggles"></div>`;

    if (r.series) {
      this._chart(wrap.querySelector(".chart"), wrap.querySelector(".tip"), r, sch, unit);
      this._legend(wrap.querySelector(".legend"), r);
      this._runtime(wrap.querySelector(".rt"), r);
      this._model(wrap.querySelector(".model"), r, unit);
    } else {
      wrap.querySelector(".chart").innerHTML = `<div class="empty">${
        r.status === "error" ? "No chart yet." : "Press Optimize to build the first forecast."
      }</div>`;
    }

    const msgs = wrap.querySelector(".msgs");
    const lines = [];
    if (r.status === "error" && r.message) lines.push(["bad", r.message]);
    if (r.status === "needs_review")
      lines.push(["warn", `The house model is off (predicted ${f1(r.validation && r.validation.ratio)}x the real AC runtime), so applying is locked. Give it a few more days of data.`]);
    if (r.status === "no_comfortable_schedule")
      lines.push(["bad", "Nothing in the search keeps you inside your comfort limits for today's forecast. Raise a limit or lower the thermostat floor."]);
    if (r.active && r.active.overshoot > 0)
      lines.push(["warn", `Today's active schedule is predicted to run ${f1(r.active.overshoot)}${unit} over your comfort limit.${r.best ? " The proposal stays inside it." : ""}`]);
    const fan = r.fan;
    if (fan && fan.enabled && fan.blocked) lines.push(["warn", `Fan circulation is paused: ${fan.blocked}.`]);
    else if (fan && fan.enabled && !fan.learned_rooms)
      lines.push(["warn", `Fan control is on, but no room has learned what the fan does yet (${f1(fan.history_fan_only_hours)} of 4 fan-only hours in history). Run the fan on circulate for a few evenings so it can learn.`]);
    if (sch.fan_note) lines.push(["warn", `Fan held at normal: ${sch.fan_note}.`]);
    if (sch.mode_note) lines.push(["warn", `${sch.mode_note}.`]);
    const pr = r.presence;
    const at = (ts) => (ts ? fmtClock(new Date(ts * 1000).getHours() * 60 + new Date(ts * 1000).getMinutes()) : "");
    if (sch.away) {
      const first = pr && (pr.people || []).filter((p) => p.return_ts === pr.return_ts)[0];
      const why = first ? ` (${this._esc(first.name)}, ${first.source === "travel" ? "travel time" : "usual time"})` : "";
      const set = [sch.away.temp !== null && sch.away.temp !== undefined ? `${f1(sch.away.temp)}${unit}` : null, sch.away.heat !== null && sch.away.heat !== undefined ? `heat ${f1(sch.away.heat)}${unit}` : null].filter(Boolean).join(", ");
      lines.push(["info", `Everyone is out: set back to ${set} until ${at(sch.away.end)}, back to comfort for ${at(sch.away.return)}${why}.`]);
    } else if (pr && pr.house_away) {
      const why = sch.guest ? "Guest mode is on" : sch.away_cancelled ? "the setpoint was changed by hand while out, so it waits until someone is home"
        : !sch.enabled ? "Run schedule is off" : !r.validated ? "the house model is not trusted yet" : !pr.window ? "someone is expected back too soon" : "it would not save anything";
      lines.push(["info", `Everyone is out, no setback: ${why}.`]);
    }
    if (r.comfort && r.comfort.recovering_min > 0)
      lines.push(["warn", `The house is outside its comfort limits right now. The plan gets it back as fast as the equipment can (about ${Math.round(r.comfort.recovering_min + 30)} min), then holds the limits again.`]);
    if (r.comfort && r.comfort.heats && !r.heat_learned && r.status !== "error")
      lines.push(["warn", "Heating has not run enough in the recent history to learn it yet. It learns as the heat runs (about 8 hours)."]);
    const gap = r.indoor_source && r.indoor_source.thermostat_gap;
    if (gap !== null && gap !== undefined && Math.abs(gap) >= (unit === "\u00b0C" ? 0.6 : 1))
      lines.push(["warn", `Your indoor sensor reads ${f1(Math.abs(gap))}${unit} ${gap < 0 ? "below" : "above"} the thermostat, and the thermostat controls to its own reading. The learned switching points account for it.`]);
    if (this._notice) lines.push(["bad", this._notice]);
    msgs.innerHTML = lines.map(([t, m]) => `<div class="msg ${t === "bad" ? "bad" : t === "info" ? "info" : ""}">${this._esc(m)}</div>`).join("");

    const nodes = wrap.querySelector(".nodes");
    const pillTemp = (n) =>
      mode === "heat" ? (n.heat !== undefined ? `heat ${f1(n.heat)}${unit}` : `${f1(n.temp)}${unit}`)
      : `${f1(n.temp)}${unit}${mode === "heat_cool" && n.heat !== undefined ? ` <span>\u00b7 heat ${f1(n.heat)}${unit}</span>` : ""}`;
    const pills = (list) =>
      (list || []).map((n) => `<span class="pill">${fmtClock(toMin(n.time))} <span>\u2192</span> ${pillTemp(n)}${n.fan ? ' <span title="Fan circulates until the next change">\u00b7 fan</span>' : ""}</span>`).join("") ||
      `<span class="pill"><span>none</span></span>`;
    const proposed = r.best && r.best.nodes;
    nodes.innerHTML = `<div class="col"><h4>Active</h4>${pills(sch.nodes)}</div>
      <div class="col prop"><h4>Proposed</h4>${proposed ? pills(proposed) : '<span class="pill"><span>none</span></span>'}</div>`;

    const canApply = !!(r.ok_to_apply && proposed && JSON.stringify(proposed) !== JSON.stringify(sch.nodes.map((n) => ({ time: n.time, temp: Number(n.temp), ...(n.heat !== undefined ? { heat: Number(n.heat) } : {}), ...(n.fan ? { fan: true } : {}) }))));
    const actions = wrap.querySelector(".actions");
    actions.innerHTML = `
      <button class="b primary" data-a="apply" ${canApply ? "" : "disabled"}>Apply proposed</button>
      <button class="b" data-a="optimize">Optimize now</button>
      <button class="b" data-a="revert" ${sch.previous ? "" : "disabled"}>Revert</button>`;
    actions.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => this._act(b.dataset.a)));

    this._learned(wrap.querySelector(".learn"), r, unit);

    const toggles = wrap.querySelector(".toggles");
    toggles.innerHTML = `
      <label class="tg"><input type="checkbox" data-t="run" ${sch.enabled ? "checked" : ""}><span class="sw"></span>Run schedule</label>
      <label class="tg"><input type="checkbox" data-t="auto_apply" ${sch.auto_apply ? "checked" : ""}><span class="sw"></span>Auto apply daily</label>
      <label class="tg"><input type="checkbox" data-t="learn" ${this._state.learn !== false ? "checked" : ""}><span class="sw"></span>Learn from my changes</label>${
        r.presence ? `<label class="tg"><input type="checkbox" data-t="guest" ${sch.guest ? "checked" : ""}><span class="sw"></span>Guest mode</label>` : ""
      }`;
    toggles.querySelectorAll("input").forEach((i) =>
      i.addEventListener("change", () => this._act("toggle", { what: i.dataset.t, value: i.checked }))
    );
  }

  _runtime(node, r) {
    const rt = r.runtime;
    if (!rt) return;
    const cur = rt.active || rt.baseline;
    const prop = rt.proposed;
    const hrs = (v) => (v === null || v === undefined ? "-" : `${f1(v)}<small>h</small>`);
    const stat = (label, v, cls) => `<div>${label}<b class="${cls || ""}">${v}</b></div>`;
    // Was the plan right? Actual against what the planning runs expected, over the same hours.
    const vs = (p) => {
      if (!p) return null;
      const d = p.planned > 0.05 ? Math.round(((p.actual - p.planned) / p.planned) * 100) : null;
      const cls = d === null ? "" : Math.abs(d) <= 20 ? "good" : Math.abs(d) <= 40 ? "warn" : "bad";
      return `${hrs(p.actual)} <small>vs ${f1(p.planned)}h pred</small>${d === null ? "" : ` <small class="${cls}">${d > 0 ? "+" : ""}${d}%</small>`}`;
    };
    const past = vs(rt.past);
    const cells = [
      past
        ? stat(rt.past.hours_covered < 23.5 ? `Last ${Math.round(rt.past.hours_covered)}h, actual vs predicted` : "Last 24h, actual vs predicted", past)
        : stat("Last 24h actual", hrs(rt.actual_24h_hours)),
      stat(rt.active ? "Next 24h, active" : "Next 24h, baseline", hrs(cur && cur.hours)),
      stat("Next 24h, proposed", hrs(prop && prop.hours), "acc"),
    ];
    const extra = (x) => (x ? `${x.cycles} cycles, longest ${Math.round(x.longest_min)} min` : "");
    const hasBars = cur && cur.minutes_per_hour && cur.minutes_per_hour.length;
    const start = Math.floor((rt.start_local_minute || 0) / 60);
    const bars = hasBars
      ? cur.minutes_per_hour
          .map((m, i) => {
            const pm = prop ? prop.minutes_per_hour[i] : 0;
            return `<div class="h" title="${fmtHour(((start + i) % 24) * 60)}: ${m} min now, ${pm} min proposed"><i style="height:${Math.max(m ? 4 : 0, (m / 60) * 100)}%"></i>${
              prop ? `<i class="p" style="height:${Math.max(pm ? 4 : 0, (pm / 60) * 100)}%"></i>` : ""
            }</div>`;
          })
          .join("")
      : "";
    const what = rt.kind === "heat" ? "heating" : "AC";
    if (rt.past_heat) cells.push(stat("Heating, actual vs predicted", vs(rt.past_heat)));
    if (rt.kind !== "heat" && cur && cur.heat_hours !== undefined)
      cells.push(stat("Heating, next 24h", `${hrs(cur.heat_hours)}${prop && prop.heat_hours !== undefined ? ` \u2192 ${hrs(prop.heat_hours)}` : ""}`));
    node.innerHTML = `<h4>${what === "AC" ? "AC" : "Heating"} runtime</h4><div class="rtgrid">${cells.join("")}</div>${
      hasBars
        ? `<div class="bars">${bars}</div><div class="barlab"><span>${fmtHour(start * 60)}</span><span>${fmtHour(((start + 6) % 24) * 60)}</span><span>${fmtHour(((start + 12) % 24) * 60)}</span><span>${fmtHour(((start + 18) % 24) * 60)}</span><span>${fmtHour(((start + 24) % 24) * 60)}</span></div>
           <div class="sub" style="margin-top:6px">${rt.past || rt.past_heat ? "On the chart, before now: filled is what ran, outlined (pred) is what the plan expected. " : ""}Minutes per hour, grey is ${rt.active ? "active" : "baseline"}, blue is proposed. ${prop ? "Proposed: " + extra(prop) + "." : ""}</div>`
        : ""
    }`;
  }

  _model(node, r, unit) {
    const bits = [];
    const c = r.control;
    if (c && c.source === "learned")
      bits.push(`Your thermostat keeps cooling until about ${f1(Math.abs(c.used_off_at))}${unit} ${c.used_off_at < 0 ? "below" : "above"} its setpoint and starts again ${f1(Math.abs(c.used_on_at))}${unit} ${c.used_on_at < 0 ? "below" : "above"} it (learned from ${c.stops} cycles)${c.setpoint_shift > 0 ? ", so proposals use setpoint numbers about " + f1(c.setpoint_shift) + unit + " higher for the same comfort" : ""}.`);
    const hc = r.heat_control;
    if (hc && hc.source === "learned")
      bits.push(`The heat starts about ${f1(Math.abs(hc.used_on_at))}${unit} ${hc.used_on_at < 0 ? "below" : "above"} its setpoint and stops ${f1(Math.abs(hc.used_off_at))}${unit} ${hc.used_off_at < 0 ? "below" : "above"} it (learned from ${hc.stops} cycles).`);
    if (r.heat_learned && r.model && r.model.h)
      bits.push(`The ${({ furnace: "furnace", electric: "electric heat", heat_pump: "heat pump", dual_fuel: "heat" })[r.heat_kind] || "heat"} warms the house about ${f1(r.model.h)}${unit} an hour while it runs.`);
    const res = r.residual;
    if (res && res.used && res.coef) {
      const parts = [];
      if (res.coef.capacity > 0.05) parts.push(`the AC loses some cooling above ${f1(res.t_cap)}${unit} outside`);
      if (res.coef.soak > 0.05) parts.push("the house holds heat after a sunny afternoon");
      if (res.coef.hot_sun > 0.05) parts.push("strong sun on hot days adds extra heat");
      if (parts.length) bits.push(`Learned from the last days: ${parts.join(", ")}.`);
    }
    const acc = r.series && r.series.accuracy;
    if (acc) bits.push(`Earlier forecasts were within ${f1(acc.mae)}${unit} of the real indoor temperature on average over the last 24h${Math.abs(acc.bias) >= 0.3 ? ` (ran ${f1(Math.abs(acc.bias))}${unit} ${acc.bias > 0 ? "warmer" : "cooler"} than predicted)` : ""}.`);
    node.innerHTML = bits.map((b) => `<div>${this._esc(b)}</div>`).join("");
  }

  _ruleText(rule, r, unit) {
    const rooms = (r.series && r.series.rooms) || [];
    const who = rule.kind === "room" ? ((rooms.find((x) => x.id === rule.room) || {}).name || rule.room) : "House";
    let when = `${fmtHour(rule.start)} to ${fmtHour(rule.end)}`;
    if (rule.start === rule.end) when = "all day";
    let cond = "";
    if (rule.cond && rule.cond.outdoor_min !== undefined) cond = ` on days over ${f1(rule.cond.outdoor_min)}${unit}`;
    if (rule.cond && rule.cond.humidity_min !== undefined) cond = ` when humidity is over ${f1(rule.cond.humidity_min)}%`;
    if (rule.cond && rule.cond.outdoor_max !== undefined) cond = ` on days under ${f1(rule.cond.outdoor_max)}${unit}`;
    return `${who} ${rule.side === "min" ? "\u2265" : "\u2264"} ${f1(rule.max)}${unit}, ${when}${cond}`;
  }

  _learned(node, r, unit) {
    const cl = r.comfort_learned;
    if (!cl || (!cl.rules.length && !cl.pending.length && !cl.notes.length)) {
      node.innerHTML = "";
      return;
    }
    const chips = cl.rules.map((rule) =>
      `<span class="rule" title="Learned from ${rule.changes} manual changes on ${rule.days} days">${this._esc(this._ruleText(rule, r, unit))} <span>\u00b7 ${rule.days} days</span><button data-forget="${this._esc(rule.id)}" title="Forget this">\u00d7</button></span>`);
    const pend = cl.pending.map((rule) =>
      `<span class="rule pending" title="Needs another day like this before it is used">${this._esc(this._ruleText(rule, r, unit))} <span>\u00b7 watching</span></span>`);
    const notes = cl.notes.map((n) => n.side === "heat"
      ? `<div class="note">You turned the heat down around ${fmtHour(n.hour * 60)} on ${n.days} days. It may be heating more than you need then.</div>`
      : `<div class="note">You turned it up around ${fmtHour(n.hour * 60)} on ${n.days} days. It may be cooling more than you need then.</div>`);
    node.innerHTML = `<h4>Learned comfort</h4>${chips.join("")}${pend.join("")}${notes.join("")}
      <div><button class="linkbtn" data-reset="1">Forget everything learned</button></div>`;
    node.querySelectorAll("[data-forget]").forEach((b) =>
      b.addEventListener("click", () => this._act("comfort", { action: "forget", rule_id: b.dataset.forget })));
    const reset = node.querySelector("[data-reset]");
    if (reset) reset.addEventListener("click", () => this._act("comfort", { action: "reset" }));
  }

  _legend(node, r) {
    const hasActive = r.series.indoor && r.series.indoor.active;
    const acc = (a) => (a ? ` <small style="opacity:.7">\u00b1${f1(a.mae)}\u00b0</small>` : "");
    const planned = [].concat(r.series.indoor_planned || [], ...(r.series.rooms || []).map((x) => x.planned || [])).some((v) => v !== null && v !== undefined);
    node.innerHTML = [
      `<span><i style="border-color:var(--hso-sun)"></i>Outdoor</span>`,
      `<span><i style="border-color:var(--primary-text-color)"></i>Indoor${acc(r.series.accuracy)}</span>`,
      ...(planned ? [`<span><i class="dash" style="border-color:var(--hso-muted);opacity:.6"></i>Predicted earlier</span>`] : []),
      ...(r.comfort && r.comfort.cools === false ? [] : [
        `<span><i style="border-color:var(--hso-muted)"></i>${hasActive ? "Active" : "Baseline"} setpoint</span>`,
        `<span><i class="dash" style="border-color:var(--hso-accent)"></i>Proposed setpoint</span>`,
        `<span><i class="box" style="background:var(--hso-cool)"></i>AC running</span>`]),
      ...(r.comfort && r.comfort.heats ? [
        `<span><i style="border-color:var(--hso-heat)"></i>Heat setpoint</span>`,
        `<span><i class="dash" style="border-color:var(--hso-heat)"></i>Proposed heat</span>`,
        `<span><i class="box" style="background:var(--hso-heat)"></i>Heating</span>`] : []),
      ...(r.series.rooms || []).map((room, k) => {
        const tag = room.trusted ? ROOM_WHEN[room.when] || room.when : "learning";
        const off = this._roomHidden(room);
        return `<span class="room ${off ? "off" : ""}" data-room="${this._esc(room.id)}" role="button" title="Tap to ${off ? "show" : "hide"}"><i style="border-color:${ROOM_COLORS[k % ROOM_COLORS.length]}"></i>${this._esc(room.name)}${acc(room.accuracy)} <small style="opacity:.7">(${this._esc(tag)})</small></span>`;
      }),
    ].join("");
    node.querySelectorAll(".room").forEach((n) =>
      n.addEventListener("click", () => {
        const room = r.series.rooms.find((x) => x.id === n.dataset.room);
        if (room) this._toggleRoom(room.id, !this._roomHidden(room));
      })
    );
  }

  // ------------------------------------------------------------------ chart
  _chart(host, tip, r, sch, unit) {
    const s = r.series;
    const n = s.outdoor.length;
    const W = 640, H = 270;
    const pad = { l: 38, r: 40, t: 12, b: 22 };
    const acH = 9, acGap = 4;
    const fanRow = [].concat(s.fan_actual || [], (s.fan && (s.fan.proposed || s.fan.active)) || []).some((v) => v);
    const heats = !!(r.comfort && r.comfort.heats);
    const cools = !(r.comfort && r.comfort.cools === false);
    const heatRow = heats || [].concat(s.heat_actual || []).some((v) => v);
    const homeRow = (s.home || []).some((v) => v !== null && v !== undefined);
    const rowsN = (cools ? 2 : 0) + (heatRow ? 2 : 0) + (fanRow ? 1 : 0) + (homeRow ? 1 : 0) || 1;
    const acArea = acH * rowsN + acGap * (rowsN - 1) + 10;
    const px0 = pad.l, px1 = W - pad.r;
    const py0 = pad.t, py1 = H - pad.b - acArea;
    const X = (i) => px0 + ((px1 - px0) * i) / (n - 1);

    const activeNodes = r.active ? sch.nodes : null;
    const lm = s.local_minutes;
    const activeSp = stepSeries(sch.nodes, lm);
    const propSp = r.best && cools ? stepSeries(r.best.nodes, lm) : null;
    const heatSp = heats ? stepSeries(sch.nodes, lm, "heat") : null;
    const heatPast = s.heat_setpoint_actual || [];
    const heatRef = heatSp && heatSp.some((v) => v !== null) ? heatSp.map((v, i) => (i < s.now_index && heatPast[i] !== null && heatPast[i] !== undefined ? heatPast[i] : v)) : (heats ? heatPast : null);
    const propHeat = heats && r.best ? stepSeries(r.best.nodes, lm, "heat") : null;
    const baseSp = !activeNodes && r.baseline ? stepSeries(r.baseline.nodes, lm) : null;
    const refSp = cools ? activeSp : lm.map(() => null); // the "current" line is the integration's active schedule
    const limitMin = s.limit_min || lm.map(() => null);
    const c = r.comfort;
    const limit = s.limit || lm.map((m) => {
      const h = m / 60;
      const inSleep = c.sleep_start > c.sleep_end ? h >= c.sleep_start || h < c.sleep_end : h >= c.sleep_start && h < c.sleep_end;
      const inAwake = c.awake_start < c.awake_end ? h >= c.awake_start && h < c.awake_end : h >= c.awake_start || h < c.awake_end;
      return inSleep ? c.sleep_max : inAwake ? c.awake_max : null;
    });

    // indoor scale (left)
    const rooms = (s.rooms || []).map((room, k) => ({ ...room, color: ROOM_COLORS[k % ROOM_COLORS.length] })).filter((room) => !this._roomHidden(room));
    const roomFuture = (room) => room.predicted.proposed || room.predicted.active || room.predicted.baseline || [];
    const ivals = [].concat(
      s.indoor_actual, s.indoor.proposed || [], s.indoor.active || s.indoor.baseline || [], refSp, propSp || [], limit,
      limitMin, heatRef || [], propHeat || [],
      s.indoor_planned || [],
      ...rooms.map((room) => room.actual.concat(roomFuture(room), room.planned || []))
    ).filter((v) => v !== null && v !== undefined);
    let imin = Math.min(...ivals) - 0.8, imax = Math.max(...ivals) + 0.8;
    if (imax - imin < 4) { const mid = (imax + imin) / 2; imin = mid - 2; imax = mid + 2; }
    const Yi = (v) => py1 - ((v - imin) / (imax - imin)) * (py1 - py0);

    // outdoor scale (right, independent)
    const ov = s.outdoor.filter((v) => v !== null);
    let omin = Math.min(...ov) - 1, omax = Math.max(...ov) + 1;
    const Yo = (v) => py1 - ((v - omin) / (omax - omin)) * (py1 - py0);

    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Schedule chart" });
    const defs = el("defs", {}, svg);
    const lg = el("linearGradient", { id: "og", x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    el("stop", { offset: "0%", "stop-color": "#ffb74d", "stop-opacity": 0.38 }, lg);
    el("stop", { offset: "100%", "stop-color": "#ffb74d", "stop-opacity": 0.02 }, lg);
    const fg = el("linearGradient", { id: "fg", x1: 0, y1: 0, x2: 1, y2: 0 }, defs);
    el("stop", { offset: "0%", "stop-color": "#7f7f7f", "stop-opacity": 0 }, fg);
    el("stop", { offset: "100%", "stop-color": "#7f7f7f", "stop-opacity": 0.1 }, fg);

    // forecast region tint
    const xNow = X(s.now_index);
    el("rect", { x: xNow, y: py0, width: px1 - xNow, height: py1 - py0 + acArea, fill: "url(#fg)" }, svg);

    // grid + left axis
    const tick = Math.max(1, Math.round((imax - imin) / 4));
    for (let v = Math.ceil(imin / tick) * tick; v <= imax; v += tick) {
      el("line", { x1: px0, x2: px1, y1: Yi(v), y2: Yi(v), stroke: "currentColor", "stroke-opacity": 0.1, "stroke-dasharray": "2 4" }, svg);
      const t = el("text", { x: px0 - 6, y: Yi(v) + 3.5, "text-anchor": "end" }, svg);
      t.textContent = Math.round(v);
    }
    // right axis (outdoor)
    const otick = Math.max(2, Math.round((omax - omin) / 4 / 2) * 2);
    for (let v = Math.ceil(omin / otick) * otick; v <= omax; v += otick) {
      const t = el("text", { x: px1 + 6, y: Yo(v) + 3.5, "text-anchor": "start", fill: "#ffb74d", style: "fill:#ffb74d;opacity:.85" }, svg);
      t.textContent = Math.round(v);
    }

    // x ticks
    for (let i = 0; i < n; i++) {
      if (lm[i] % 360 === 0) {
        el("line", { x1: X(i), x2: X(i), y1: py0, y2: py1 + acArea, stroke: "currentColor", "stroke-opacity": 0.07 }, svg);
        const t = el("text", { x: X(i), y: H - 6, "text-anchor": "middle" }, svg);
        t.textContent = fmtHour(lm[i]);
      }
    }

    // outdoor area + line
    const opts = s.outdoor.map((v, i) => (v === null ? null : [X(i), Yo(v)])).filter(Boolean);
    if (opts.length > 1) {
      const d = opts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
      el("path", { d: `${d}L${opts[opts.length - 1][0]},${py1}L${opts[0][0]},${py1}Z`, fill: "url(#og)" }, svg);
      el("path", { d, fill: "none", stroke: "#ffb74d", "stroke-width": 1.4, "stroke-opacity": 0.75, "stroke-linejoin": "round" }, svg);
    }

    // comfort limits: red stay-below (cooling), blue stay-above (heating)
    this._line(svg, limit, X, Yi, { stroke: "#e53935", "stroke-width": 1.2, "stroke-opacity": 0.55, "stroke-dasharray": "1 3" }, true);
    this._line(svg, limitMin, X, Yi, { stroke: "#1e88e5", "stroke-width": 1.2, "stroke-opacity": 0.6, "stroke-dasharray": "1 3" }, true);
    if (heatRef) this._line(svg, heatRef, X, Yi, { stroke: "var(--hso-heat, #ff7043)", "stroke-opacity": 0.7, "stroke-width": 1.8 }, true);
    if (propHeat) this._line(svg, propHeat, X, Yi, { stroke: "var(--hso-heat, #ff7043)", "stroke-width": 2, "stroke-dasharray": "6 4" }, true);

    // setpoints
    this._line(svg, refSp, X, Yi, { stroke: "currentColor", "stroke-opacity": 0.5, "stroke-width": 1.8 }, true);
    if (baseSp) this._line(svg, baseSp, X, Yi, { stroke: "currentColor", "stroke-opacity": 0.35, "stroke-width": 1.4 }, true);
    if (propSp) this._line(svg, propSp, X, Yi, { stroke: "var(--hso-accent, #03a9f4)", "stroke-width": 2, "stroke-dasharray": "6 4" }, true);

    // rooms: actual (past) solid, predicted (future, under the proposal when there is one) dashed
    rooms.forEach((room) => {
      const col = room.color;
      if (room.planned) this._line(svg, room.planned, X, Yi, { stroke: col, "stroke-width": 1.2, "stroke-opacity": 0.35, "stroke-dasharray": "3 3" }, false);
      this._line(svg, room.actual, X, Yi, { stroke: col, "stroke-width": 1.5, "stroke-opacity": 0.85 }, false);
      this._line(svg, roomFuture(room), X, Yi, { stroke: col, "stroke-width": 1.5, "stroke-opacity": room.trusted ? 0.85 : 0.4, "stroke-dasharray": "4 3" }, false);
    });

    // indoor: actual (past) solid, predicted (future) under each schedule
    const ia = s.indoor_actual;
    if (s.indoor_planned) this._line(svg, s.indoor_planned, X, Yi, { stroke: "var(--primary-text-color, #212121)", "stroke-width": 1.4, "stroke-opacity": 0.4, "stroke-dasharray": "3 3" }, false);
    this._line(svg, ia, X, Yi, { stroke: "var(--primary-text-color, #212121)", "stroke-width": 2.2 }, false);

    // manual setpoint changes (what comfort learning reads)
    (s.manual || []).forEach((m) => {
      const x = X(m.i), y = Yi(m.to);
      el("path", { d: `M${(x - 4).toFixed(1)},${(y - 7).toFixed(1)}L${(x + 4).toFixed(1)},${(y - 7).toFixed(1)}L${x.toFixed(1)},${(y - 1).toFixed(1)}Z`, fill: "var(--hso-warn, #f9a825)", "fill-opacity": 0.9 }, svg);
    });
    const futureRef = (s.indoor.active || s.indoor.baseline);
    if (futureRef) this._line(svg, futureRef, X, Yi, { stroke: "var(--primary-text-color, #212121)", "stroke-width": 1.4, "stroke-opacity": 0.45, "stroke-dasharray": "2 3" }, false);
    if (s.indoor.proposed) this._line(svg, s.indoor.proposed, X, Yi, { stroke: "var(--hso-accent, #03a9f4)", "stroke-width": 2.2 }, false);

    // equipment strips: AC (now / proposed), heat (now / proposed), fan
    const bw = (px1 - px0) / (n - 1);
    const lab = (txt, y, right) => {
      const t = el("text", right ? { x: px1 + 4, y: y + 7.5, "text-anchor": "start" } : { x: px0 - 6, y: y + 7.5, "text-anchor": "end" }, svg);
      t.textContent = txt;
    };
    let ry = py1 + 10;
    // past: what ran (filled) or, with predicted, what the plan expected (outlined); future: the forecast
    const strip = (label, past, fut, color, solid, predicted, rightLabel) => {
      for (let i = 0; i < n; i++) {
        const isPast = i < s.now_index;
        const v = isPast ? (past ? past[i] : null) : fut[i];
        if (!v) continue;
        if (isPast && predicted) {
          const h = Math.max(2, acH * Math.min(1, v));
          el("rect", { x: X(i) - bw / 2 + 0.4, y: ry + acH - h + 0.4, width: Math.max(0.5, bw - 0.4), height: h - 0.8, fill: color, "fill-opacity": 0.12, stroke: color, "stroke-width": 0.8, "stroke-opacity": 0.9 }, svg);
        } else el("rect", { x: X(i) - bw / 2, y: ry, width: bw + 0.4, height: acH, rx: 1.5, fill: color, "fill-opacity": isPast || !solid ? 0.25 + 0.7 * Math.min(1, v) : 0.85 }, svg);
      }
      lab(label, ry);
      if (rightLabel) lab(rightLabel, ry, true);
      ry += acH + acGap;
    };
    const anyPlan = (a) => (a || []).some((v) => v !== null && v !== undefined);
    if (cools) {
      const pred = anyPlan(s.ac_planned);
      strip("AC", s.ac_actual, s.ac.active || s.ac.baseline || [], "#4aa3ff", false);
      strip(pred ? "pred" : "new", pred ? s.ac_planned : null, s.ac.proposed || [], "var(--hso-accent, #03a9f4)", true, true, pred && s.ac.proposed ? "new" : null);
    }
    if (heatRow) {
      const hf = (s.heat && (s.heat.active || s.heat.baseline)) || [];
      const pred = anyPlan(s.heat_planned);
      strip("heat", s.heat_actual || [], hf, "#ff7043", false);
      strip(pred ? "pred" : "new", pred ? s.heat_planned : null, (s.heat && s.heat.proposed) || [], "#ff7043", true, true, pred && s.heat && s.heat.proposed ? "new" : null);
    }
    if (fanRow) strip("fan", s.fan_actual || [], s.fan.proposed || s.fan.active || s.fan.baseline || [], "#26a69a", true);
    if (homeRow) {
      const out = s.home.map((v) => (v === 0 ? 1 : 0));
      strip("away", out, out, "var(--secondary-text-color, #727272)", false);
    }

    // now marker
    el("line", { x1: xNow, x2: xNow, y1: py0 - 4, y2: py1 + acArea, stroke: "var(--primary-text-color, #212121)", "stroke-opacity": 0.5, "stroke-width": 1 }, svg);
    const nt = el("text", { x: xNow + 4, y: py0 + 6, "text-anchor": "start" }, svg);
    nt.textContent = "now";

    // hover
    const cross = el("line", { y1: py0, y2: py1 + acArea, stroke: "currentColor", "stroke-opacity": 0.35, visibility: "hidden" }, svg);
    const hit = el("rect", { x: px0, y: py0, width: px1 - px0, height: py1 - py0 + acArea, fill: "transparent" }, svg);
    const move = (ev) => {
      const box = svg.getBoundingClientRect();
      const sx = ((ev.clientX - box.left) / box.width) * W;
      const i = Math.max(0, Math.min(n - 1, Math.round(((sx - px0) / (px1 - px0)) * (n - 1))));
      cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i)); cross.setAttribute("visibility", "visible");
      const future = i >= s.now_index;
      const withPred = (v, p) => {
        if (future || p === null || p === undefined || v === null || v === undefined) return `${f1(v)}${unit}`;
        const d = v - p;
        return `${f1(v)}${unit} <span style="opacity:.65">pred ${f1(p)} (${d >= 0 ? "+" : ""}${f1(d)})</span>`;
      };
      const rows = [
        ["Outdoor", `${f1(s.outdoor[i])}${unit}`],
        [future ? "Indoor (active)" : "Indoor", future ? `${f1((s.indoor.active || s.indoor.baseline || [])[i])}${unit}` : withPred(ia[i], (s.indoor_planned || [])[i])],
      ];
      if (future && s.indoor.proposed) rows.push(["Indoor (proposed)", `${f1(s.indoor.proposed[i])}${unit}`]);
      rooms.forEach((room) => {
        const v = future ? roomFuture(room)[i] : room.actual[i];
        if (v !== null && v !== undefined) rows.push([room.name, withPred(v, (room.planned || [])[i])]);
      });
      if (cools) rows.push(["Setpoint", `${f1(refSp[i])}${unit}`]);
      if (propSp) rows.push(["Proposed", `${f1(propSp[i])}${unit}`]);
      if (heatRef) rows.push(["Heat setpoint", `${f1(heatRef[i])}${unit}`]);
      if (propHeat) rows.push(["Proposed heat", `${f1(propHeat[i])}${unit}`]);
      const onOff = (v) => (v === null || v === undefined ? "-" : v >= 0.5 ? "on" : v > 0 ? `${Math.round(v * 100)}%` : "off");
      const withPlan = (v, p) => (future || p === null || p === undefined ? onOff(v) : `${onOff(v)} <span style="opacity:.65">pred ${onOff(p)}</span>`);
      if (cools) rows.push(["AC", withPlan(future ? (s.ac.active || s.ac.baseline || [])[i] : s.ac_actual[i], (s.ac_planned || [])[i])]);
      if (heatRow) rows.push(["Heat", withPlan(future ? ((s.heat && (s.heat.active || s.heat.baseline)) || [])[i] : (s.heat_actual || [])[i], (s.heat_planned || [])[i])]);
      if (homeRow && s.home[i] !== null && s.home[i] !== undefined) rows.push([future ? "Home (expected)" : "Home", s.home[i] ? "someone" : "everyone out"]);
      const man = (s.manual || []).find((m) => m.i === i);
      if (man) rows.push(["You changed", `${f1(man.from)} \u2192 ${f1(man.to)}${unit}`]);
      if (fanRow) {
        const fv = future ? (s.fan.proposed || s.fan.active || [])[i] : (s.fan_actual || [])[i];
        rows.push([future ? "Fan (proposed)" : "Fan", fv === null || fv === undefined ? "-" : fv >= 0.5 ? "circulate" : "normal"]);
      }
      tip.innerHTML = `<b>${fmtClock(lm[i])}</b>${future ? " \u00b7 forecast" : ""}` +
        rows.map(([k, v]) => `<div class="r"><span>${k}</span><span>${v}</span></div>`).join("");
      const hostBox = host.getBoundingClientRect();
      const wantRight = X(i) / W < 0.55;
      const left = (X(i) / W) * hostBox.width + (wantRight ? 14 : -14 - 150);
      tip.style.left = `${Math.max(0, Math.min(hostBox.width - 160, left))}px`;
      tip.style.top = "8px";
      tip.classList.add("on");
    };
    const leave = () => { cross.setAttribute("visibility", "hidden"); tip.classList.remove("on"); };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", leave);
    host.innerHTML = "";
    host.appendChild(svg);
  }

  // Step or smooth line that breaks at null gaps.
  _line(svg, values, X, Y, attrs, step) {
    let d = "";
    let pen = false;
    let prevY = null;
    values.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; prevY = null; return; }
      const x = X(i), y = Y(v);
      if (!pen) d += `M${x.toFixed(1)},${y.toFixed(1)}`;
      else if (step) d += `L${x.toFixed(1)},${prevY.toFixed(1)}L${x.toFixed(1)},${y.toFixed(1)}`;
      else d += `L${x.toFixed(1)},${y.toFixed(1)}`;
      pen = true; prevY = y;
    });
    if (d) el("path", { d, fill: "none", "stroke-linejoin": "round", "stroke-linecap": "round", ...attrs }, svg);
  }

  _esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
}

if (!customElements.get("hvac-schedule-card")) customElements.define("hvac-schedule-card", HvacScheduleCard);
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "hvac-schedule-card")) {
  window.customCards.push({
    type: "hvac-schedule-card",
    name: "HVAC Schedule",
    description: "Active and proposed cooling schedule over the weather forecast, with apply and revert.",
    preview: false,
  });
}
console.info(`%c HVAC-SCHEDULE-CARD %c ${CARD_VERSION} `, "background:#03a9f4;color:#fff;border-radius:3px 0 0 3px", "background:#333;color:#fff;border-radius:0 3px 3px 0");
