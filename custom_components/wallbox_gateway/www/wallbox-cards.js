/*! Wallbox Gateway — native Lovelace cards.
 *
 * Ships with the Wallbox Gateway integration and is auto-registered by it
 * (see frontend.py), so these cards appear in the Lovelace card picker with
 * no manual resource setup. Vanilla web components — no build step.
 *
 * Cards:
 *   custom:wallbox-energy-flow   Solar/Grid → Vehicle power-flow viz
 *   custom:wallbox-controls      Start/Stop, max current, solar, resume
 *   custom:wallbox-status        Compact status + quick stats (glance)
 *   custom:wallbox-sessions      Session energy + solar/grid split
 *
 * Each card auto-finds the single Wallbox device (or takes `device:` in its
 * config for multi-charger setups) and resolves entities by (domain, key)
 * from the frontend entity registry — robust to entity_id renames.
 */
(function () {
  "use strict";

  var VERSION = "0.23.0";

  // ---- shared base -------------------------------------------------------

  class WBBase extends HTMLElement {
    setConfig(config) {
      this._config = config || {};
      this._built = false;
    }
    set hass(hass) {
      this._hass = hass;
      if (!this._built) {
        // Only latch as built once the DOM is really constructed. If the device
        // wasn't resolvable yet (registry not populated, or multi-device without
        // `device:`), _build() returns false and we retry on the next hass — so
        // Controls / Energy-flow (whose _update only mutates existing nodes)
        // recover instead of staying stuck on the error text.
        this._built = this._build() === true;
      }
      this._update();
    }
    getCardSize() { return 3; }

    // Resolve the Wallbox device id: explicit config.device, else the single
    // wallbox_gateway device. Caches the id and re-derives _devErr fresh each
    // call (never sticky). Returns null (with this._devErr set) otherwise.
    _deviceId() {
      var hass = this._hass;
      if (!hass) return null;
      this._devErr = null;
      if (this._config.device) return this._config.device;
      if (this._devId && hass.devices && hass.devices[this._devId]) return this._devId;
      var devs = hass.devices ? Object.values(hass.devices) : [];
      var wb = devs.filter(function (d) {
        return (d.identifiers || []).some(function (i) { return i[0] === "wallbox_gateway"; });
      });
      if (wb.length === 1) { this._devId = wb[0].id; return this._devId; }
      this._devId = null;
      if (wb.length > 1) { this._devErr = "multiple"; return null; }
      this._devErr = "none";
      return null;
    }

    // Resolve entity_id by (domain, key) within the Wallbox device. Prefers a
    // translation_key match; falls back to an entity_id suffix match, taking the
    // SHORTEST match so a shorter key can't be captured by a longer entity (e.g.
    // key "charging" must not grab switch...solar_charging). Memoized per card.
    _eid(domain, key) {
      var hass = this._hass;
      var dev = this._deviceId();
      if (!hass || !dev || !hass.entities) return null;
      var ck = domain + "." + key;
      this._eidCache = this._eidCache || {};
      var cached = this._eidCache[ck];
      if (cached) {
        var ce = hass.entities[cached];
        if (ce && ce.device_id === dev) return cached;
      }
      var ents = Object.values(hass.entities).filter(function (e) {
        return e.device_id === dev && e.platform === "wallbox_gateway" &&
          e.entity_id.indexOf(domain + ".") === 0;
      });
      var hit = ents.find(function (e) { return e.translation_key === key; });
      if (!hit) {
        var suf = "_" + key;
        var cands = ents.filter(function (e) { return e.entity_id.slice(-suf.length) === suf; });
        cands.sort(function (a, b) { return a.entity_id.length - b.entity_id.length; });
        hit = cands[0];
      }
      var id = hit ? hit.entity_id : null;
      if (id) this._eidCache[ck] = id;
      return id;
    }
    _st(domain, key) {
      var id = this._eid(domain, key);
      return id ? this._hass.states[id] : null;
    }
    _num(domain, key) {
      var s = this._st(domain, key);
      if (!s) return null;
      var n = parseFloat(s.state);
      return isNaN(n) ? null : n;
    }
    _call(domain, service, data) {
      if (this._hass) this._hass.callService(domain, service, data);
    }

    // <ha-card> wrapper with shared styling; subclasses fill `.wb-body`.
    // attachShadow is guarded so a rebuild (after the device becomes
    // resolvable) doesn't throw "shadow root already attached".
    _shell(title) {
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML =
        "<style>" + WBBase.styles + "</style>" +
        '<ha-card>' +
          (title ? '<div class="wb-hd">' + esc(title) + "</div>" : "") +
          '<div class="wb-body"></div>' +
        "</ha-card>";
      return this.shadowRoot.querySelector(".wb-body");
    }
    _err(msg) {
      var b = this._body;
      if (b) b.innerHTML = '<div class="wb-err">' + esc(msg) + "</div>";
    }
    _deviceMissing() {
      var id = this._deviceId();  // resolve first so _devErr is fresh
      if (this._devErr === "multiple") {
        this._err("Multiple Wallbox chargers found — set `device:` in the card config.");
        return true;
      }
      if (!id) {
        this._err("No Wallbox Gateway device found.");
        return true;
      }
      return false;
    }
  }

  WBBase.styles = [
    "ha-card{padding:16px;height:100%;box-sizing:border-box}",
    ".wb-hd{font-size:1.05rem;font-weight:600;margin:0 0 12px;color:var(--primary-text-color)}",
    ".wb-err{color:var(--error-color,#db4437);font-size:.9rem;padding:6px 0}",
    ".wb-muted{color:var(--secondary-text-color)}",
    // stat row
    ".wb-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(84px,1fr));gap:10px}",
    ".wb-stat{background:var(--secondary-background-color);border-radius:10px;padding:10px 8px;text-align:center}",
    ".wb-stat .v{font-size:1.25rem;font-weight:700;color:var(--primary-text-color);font-variant-numeric:tabular-nums}",
    ".wb-stat .u{font-size:.72rem;color:var(--secondary-text-color);margin-left:2px}",
    ".wb-stat .l{font-size:.7rem;color:var(--secondary-text-color);text-transform:uppercase;letter-spacing:.03em;margin-top:3px}",
    // status pill
    ".wb-status{display:flex;align-items:center;gap:10px;margin-bottom:12px}",
    ".wb-dot{width:10px;height:10px;border-radius:50%;background:var(--disabled-text-color,#888);flex:none}",
    ".wb-dot.on{background:var(--success-color,#43a047)}",
    ".wb-dot.charging{background:var(--primary-color);box-shadow:0 0 0 4px rgba(3,169,244,.18)}",
    ".wb-status .t{font-weight:600;color:var(--primary-text-color)}",
    // controls
    ".wb-btns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}",
    ".wb-btn{border:none;border-radius:10px;padding:11px;font-size:.92rem;font-weight:600;cursor:pointer;background:var(--secondary-background-color);color:var(--primary-text-color)}",
    ".wb-btn:hover{filter:brightness(1.08)}",
    ".wb-btn.go{background:var(--success-color,#43a047);color:#fff}",
    ".wb-btn.stop{background:var(--error-color,#db4437);color:#fff}",
    ".wb-row{display:flex;align-items:center;gap:12px;margin:10px 0}",
    ".wb-row label{flex:none;color:var(--secondary-text-color);font-size:.85rem;min-width:82px}",
    ".wb-row input[type=range]{flex:1;accent-color:var(--primary-color)}",
    ".wb-row .val{flex:none;font-weight:700;color:var(--primary-text-color);min-width:44px;text-align:right}",
    ".wb-toggle{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:6px}",
    ".wb-sw{position:relative;width:44px;height:24px;flex:none}",
    ".wb-sw input{opacity:0;width:0;height:0}",
    ".wb-sl{position:absolute;inset:0;border-radius:24px;background:var(--disabled-text-color,#888);cursor:pointer;transition:background .15s}",
    ".wb-sl::before{content:'';position:absolute;width:18px;height:18px;left:3px;top:3px;border-radius:50%;background:#fff;transition:transform .15s}",
    ".wb-sw input:checked + .wb-sl{background:var(--success-color,#43a047)}",
    ".wb-sw input:checked + .wb-sl::before{transform:translateX(20px)}",
    // split bar
    ".wb-bar{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--secondary-background-color);margin:10px 0 6px}",
    ".wb-bar .solar{background:#f59e0b}",
    ".wb-bar .grid{background:var(--primary-color)}",
    ".wb-legend{display:flex;gap:16px;font-size:.78rem;color:var(--secondary-text-color)}",
    ".wb-legend .k{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:baseline}",
    ".wb-hero{text-align:center;margin:4px 0 8px}",
    ".wb-hero .v{font-size:2rem;font-weight:800;color:var(--primary-text-color)}",
    ".wb-hero .u{font-size:.9rem;color:var(--secondary-text-color);margin-left:4px}",
    ".wb-hero .l{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:var(--secondary-text-color)}",
    // energy flow
    ".wb-pf{width:100%;height:auto;display:block}",
    ".wb-pf-status{text-align:center;font-size:.85rem;color:var(--secondary-text-color);margin-top:8px}",
  ].join("");

  // ---- helpers -----------------------------------------------------------

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function fmt(n, d) {
    if (n == null) return "--";
    return Number(n).toFixed(d == null ? 1 : d);
  }

  // ---- status card -------------------------------------------------------

  class WBStatus extends WBBase {
    _build() { this._body = this._shell(this._config.title || "Wallbox"); return true; }
    _update() {
      if (!this._body || this._deviceMissing()) return;
      var status = this._st("sensor", "charger_status");
      var charging = this._st("binary_sensor", "charging");
      var connected = this._st("binary_sensor", "car_connected");
      var power = this._num("sensor", "charging_power");
      var energy = this._num("sensor", "session_energy");
      var cur = this._num("sensor", "max_charging_current");

      var isCharging = charging && charging.state === "on";
      var isConn = connected && connected.state === "on";
      var dot = isCharging ? "charging" : (isConn ? "on" : "");
      var label = status ? status.state : (isCharging ? "Charging" : (isConn ? "Connected" : "Idle"));

      this._body.innerHTML =
        '<div class="wb-status"><span class="wb-dot ' + dot + '"></span>' +
          '<span class="t">' + esc(label) + "</span></div>" +
        '<div class="wb-stats">' +
          stat(fmt(power), "kW", "Power") +
          stat(fmt(energy, 2), "kWh", "Session") +
          stat(cur == null ? "--" : fmt(cur, 0), "A", "Max curr") +
        "</div>";
    }
  }

  function stat(v, u, l) {
    return '<div class="wb-stat"><div class="v">' + esc(v) +
      '<span class="u">' + esc(u) + '</span></div><div class="l">' + esc(l) + "</div></div>";
  }

  // ---- controls card -----------------------------------------------------

  class WBControls extends WBBase {
    _build() {
      this._body = this._shell(this._config.title || "Wallbox controls");
      if (this._deviceMissing()) return false;
      this._body.innerHTML =
        '<div class="wb-btns">' +
          '<button class="wb-btn go" data-act="start">▶ Start</button>' +
          '<button class="wb-btn stop" data-act="stop">⏹ Stop</button>' +
        "</div>" +
        '<div class="wb-row"><label>Max current</label>' +
          '<input type="range" class="wb-cur" min="6" max="32" step="1">' +
          '<span class="val"><span class="wb-cur-v">--</span> A</span></div>' +
        '<div class="wb-toggle wb-solar-wrap" hidden><span class="wb-muted">Solar charging</span>' +
          '<label class="wb-sw"><input type="checkbox" class="wb-solar"><span class="wb-sl"></span></label></div>' +
          '<button class="wb-btn wb-resume" data-act="resume" style="width:100%;margin-top:12px" hidden>▶ Resume schedule</button>';

      var self = this;
      this._body.querySelectorAll(".wb-btn[data-act]").forEach(function (b) {
        b.addEventListener("click", function () { self._action(b.getAttribute("data-act")); });
      });
      var slider = this._body.querySelector(".wb-cur");
      slider.addEventListener("input", function () {
        self._body.querySelector(".wb-cur-v").textContent = slider.value;
        self._dragging = true;
      });
      slider.addEventListener("change", function () {
        self._dragging = false;
        // Remember what we asked for and stop _update() from snapping the
        // slider back to the (still-stale) entity value until the gateway
        // reflects it — otherwise the control visibly bounces on the next poll.
        self._pending = Number(slider.value);
        clearTimeout(self._pendT);
        self._pendT = setTimeout(function () { self._pending = null; }, 8000);
        var id = self._eid("number", "max_current");
        if (id) self._call("number", "set_value", { entity_id: id, value: Number(slider.value) });
      });
      this._body.querySelector(".wb-solar").addEventListener("change", function (e) {
        var id = self._eid("switch", "solar_charging");
        if (id) self._call("switch", e.target.checked ? "turn_on" : "turn_off", { entity_id: id });
      });
      return true;
    }
    _action(act) {
      if (act === "start" || act === "stop") {
        var id = this._eid("switch", "charging");
        if (id) this._call("switch", act === "start" ? "turn_on" : "turn_off", { entity_id: id });
      } else if (act === "resume") {
        var rid = this._eid("button", "resume_schedule");
        if (rid) this._call("button", "press", { entity_id: rid });
      }
    }
    _update() {
      if (!this._body || this._deviceMissing()) return;
      var numState = this._st("number", "max_current");
      var slider = this._body.querySelector(".wb-cur");
      if (numState && slider && !this._dragging) {
        var a = numState.attributes || {};
        if (a.min != null) slider.min = a.min;
        if (a.max != null) slider.max = a.max;
        if (a.step != null) slider.step = a.step;
        var v = parseFloat(numState.state);
        if (!isNaN(v)) {
          if (this._pending != null && v !== this._pending) {
            // waiting for the gateway to reflect our set — keep the user's value
          } else {
            this._pending = null;
            slider.value = v;
            this._body.querySelector(".wb-cur-v").textContent = String(Math.round(v));
          }
        }
      }
      // Solar toggle only if the charger exposes it.
      var solar = this._st("switch", "solar_charging");
      var wrap = this._body.querySelector(".wb-solar-wrap");
      if (wrap) {
        wrap.hidden = !solar;
        if (solar) this._body.querySelector(".wb-solar").checked = solar.state === "on";
      }
      // Resume button only when the native schedule is paused (resume button
      // exists always; show it when charging is externally controlled). Keep
      // it simple: always show the resume button if the entity exists.
      var rbtn = this._body.querySelector(".wb-resume");
      if (rbtn) rbtn.hidden = !this._eid("button", "resume_schedule");
    }
  }

  // ---- sessions card -----------------------------------------------------

  class WBSessions extends WBBase {
    _build() { this._body = this._shell(this._config.title || "Charging session"); return true; }
    _update() {
      if (!this._body || this._deviceMissing()) return;
      var energy = this._num("sensor", "session_energy");
      var green = this._num("sensor", "green_energy_session");
      var grid = this._num("sensor", "grid_energy_session");
      var total = (green || 0) + (grid || 0);
      var haveSplit = total > 0;
      var gp = haveSplit ? (green || 0) / total * 100 : 0;
      var rp = haveSplit ? (grid || 0) / total * 100 : 0;

      var html =
        '<div class="wb-hero"><div class="l">This session</div>' +
          '<div><span class="v">' + fmt(energy, 2) + '</span><span class="u">kWh</span></div></div>';
      if (haveSplit) {
        html +=
          '<div class="wb-bar"><div class="solar" style="width:' + gp.toFixed(0) + '%"></div>' +
            '<div class="grid" style="width:' + rp.toFixed(0) + '%"></div></div>' +
          '<div class="wb-legend">' +
            '<span><span class="k" style="background:#f59e0b"></span>Solar ' + fmt(green, 2) + " kWh</span>" +
            '<span><span class="k" style="background:var(--primary-color)"></span>Grid ' + fmt(grid, 2) + " kWh</span>" +
          "</div>";
      } else {
        html += '<div class="wb-muted" style="text-align:center;font-size:.82rem">No solar/grid split yet this session.</div>';
      }
      this._body.innerHTML = html;
    }
  }

  // ---- energy-flow card --------------------------------------------------

  var PF_SVG =
    '<svg viewBox="0 0 320 192" class="wb-pf" role="img" aria-label="Energy flow: Solar and Grid to Vehicle">' +
      '<path d="M160,74 C160,116 214,122 246,130" fill="none" stroke="var(--divider-color)" stroke-width="2" stroke-dasharray="5 4"/>' +
      '<line x1="76" y1="140" x2="244" y2="140" stroke="var(--divider-color)" stroke-width="2" stroke-dasharray="5 4"/>' +
      '<path class="pf-solar-line" d="M160,74 C160,116 214,122 246,130" fill="none" stroke="#f59e0b" stroke-width="3.5" stroke-dasharray="9 6" style="opacity:0"/>' +
      '<line class="pf-grid-line" x1="76" y1="140" x2="244" y2="140" stroke="var(--primary-color)" stroke-width="3.5" stroke-dasharray="9 6" style="opacity:0"/>' +
      '<text class="pf-live" x="158" y="112" text-anchor="middle" font-size="12" fill="var(--primary-text-color)" font-weight="700"></text>' +
      '<g><circle cx="160" cy="44" r="30" fill="rgba(245,158,11,.10)" stroke="#f59e0b" stroke-width="1.6"/>' +
        '<text x="160" y="40" text-anchor="middle" font-size="18">☀️</text>' +
        '<text class="pf-solar-kwh" x="160" y="58" text-anchor="middle" font-size="11" fill="var(--primary-text-color)" font-weight="700">--</text>' +
        '<text x="160" y="88" text-anchor="middle" font-size="11" fill="var(--secondary-text-color)">Solar</text></g>' +
      '<g><circle cx="46" cy="140" r="30" fill="var(--secondary-background-color)" stroke="var(--primary-color)" stroke-width="1.6"/>' +
        '<text x="46" y="136" text-anchor="middle" font-size="18">🏭</text>' +
        '<text class="pf-grid-kwh" x="46" y="156" text-anchor="middle" font-size="11" fill="var(--primary-text-color)" font-weight="700">--</text>' +
        '<text x="46" y="186" text-anchor="middle" font-size="11" fill="var(--secondary-text-color)">Grid</text></g>' +
      '<g><circle cx="274" cy="140" r="32" fill="var(--secondary-background-color)" stroke="var(--divider-color)" stroke-width="1.6"/>' +
        '<text x="274" y="136" text-anchor="middle" font-size="20">🚗</text>' +
        '<text class="pf-car-kwh" x="274" y="156" text-anchor="middle" font-size="11" fill="var(--primary-text-color)" font-weight="700">--</text>' +
        '<text class="pf-plugin" x="274" y="156" text-anchor="middle" font-size="10" fill="var(--primary-color)" font-weight="700" style="display:none">🔌 Plug in</text>' +
        '<text x="274" y="186" text-anchor="middle" font-size="11" fill="var(--secondary-text-color)">Vehicle</text></g>' +
    "</svg>";

  class WBEnergyFlow extends WBBase {
    _build() {
      this._body = this._shell(this._config.title || "Energy flow");
      if (this._deviceMissing()) return false;
      this._body.innerHTML = PF_SVG + '<div class="wb-pf-status">Connecting…</div>';
      return true;
    }
    _update() {
      if (!this._body || this._deviceMissing()) return;
      var q = this._body.querySelector.bind(this._body);
      var power = this._num("sensor", "charging_power");
      var green = this._num("sensor", "green_energy_session");
      var grid = this._num("sensor", "grid_energy_session");
      var session = this._num("sensor", "session_energy");
      var connected = this._st("binary_sensor", "car_connected");
      var charging = this._st("binary_sensor", "charging");
      var isCharging = charging && charging.state === "on" && power && power > 0.05;
      var isConn = connected ? connected.state === "on" : true;

      var solarLine = q(".pf-solar-line"), gridLine = q(".pf-grid-line");
      var usingSolar = (green || 0) > 0;
      if (solarLine) solarLine.style.opacity = isCharging && usingSolar ? "1" : "0";
      if (gridLine) gridLine.style.opacity = isCharging ? "1" : "0";

      var live = q(".pf-live");
      if (live) live.textContent = isCharging ? fmt(power) + " kW" : "";
      set(q(".pf-solar-kwh"), green == null ? "--" : fmt(green, 1));
      set(q(".pf-grid-kwh"), grid == null ? "--" : fmt(grid, 1));

      var carKwh = q(".pf-car-kwh"), plugin = q(".pf-plugin");
      if (isConn) {
        if (carKwh) { carKwh.style.display = ""; set(carKwh, session == null ? "--" : fmt(session, 1)); }
        if (plugin) plugin.style.display = "none";
      } else {
        if (carKwh) carKwh.style.display = "none";
        if (plugin) plugin.style.display = "";
      }

      var st = q(".wb-pf-status");
      if (st) {
        st.textContent = !isConn ? "Vehicle not connected" :
          (isCharging ? ("Charging" + (usingSolar ? " · using solar" : "")) : "Connected — not charging");
      }
    }
  }
  function set(el, v) { if (el) el.textContent = v; }

  // ---- register ----------------------------------------------------------

  var CARDS = [
    ["wallbox-energy-flow", WBEnergyFlow, "Wallbox Energy Flow", "Solar/Grid → Vehicle live power flow."],
    ["wallbox-controls", WBControls, "Wallbox Controls", "Start/stop, max current, solar charging."],
    ["wallbox-status", WBStatus, "Wallbox Status", "Charger status + quick stats."],
    ["wallbox-sessions", WBSessions, "Wallbox Session", "Session energy + solar/grid split."],
  ];
  CARDS.forEach(function (c) {
    if (!customElements.get(c[0])) customElements.define(c[0], c[1]);
  });
  window.customCards = window.customCards || [];
  CARDS.forEach(function (c) {
    window.customCards.push({
      type: c[0], name: c[2], description: c[3], preview: true,
      documentationURL: "https://github.com/botts7/hass-wallbox-gateway",
    });
  });

  // eslint-disable-next-line no-console
  console.info("%c WALLBOX-CARDS %c v" + VERSION + " ",
    "color:#fff;background:#03a9f4;border-radius:3px 0 0 3px",
    "color:#03a9f4;background:#111;border-radius:0 3px 3px 0");
})();
