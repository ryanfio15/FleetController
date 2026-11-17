/* FleetController web UI — three views over fleet-api (plan §6.7).
 *
 *   #/fleet            status grid, one card per vehicle
 *   #/vehicle/<id>     gauges, downsampled charts, trips, alerts, Grafana
 *   #/alerts           inbox with acknowledge
 *
 * No framework and no build step. The whole file is read once, served as one
 * static asset, and is small enough that the absence of a bundler costs
 * nothing. See index.html for why there is no CDN either.
 *
 * Two rules it keeps:
 *
 *   1. Nothing goes into the DOM as HTML unless it came from this file.
 *      `vehicle_id` equals a certificate CN and `label` is operator-supplied,
 *      so both are escaped (`esc`). A fleet dashboard that can be XSS'd by
 *      naming a car is a poor advertisement for the security chapter.
 *   2. The API path is relative. The page is served at `/` and the API at
 *      `/api`, through one ingress-nginx, so an absolute origin would break
 *      the moment the tunnel's local port changed.
 */

'use strict';

const API = '/api/v1';

// Poll rather than stream. The pipeline's SLO is "visible in under 5 seconds"
// (§3 Stage 0), so a 5 s poll is at the resolution the system actually
// promises; a WebSocket would be a second transport to secure and operate for
// no gain the exit criterion can see.
const POLL_MS = 5000;
const SERIES_POLL_MS = 15000;

const state = {
  role: 'admin',
  userId: '11111111-1111-1111-1111-111111111111',
  timers: [],
};

// --------------------------------------------------------------------- util

const el = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function num(value, digits = 1) {
  if (value === null || value === undefined) return '—';
  return Number(value).toFixed(digits);
}

/* "4s ago" / "12m ago". The API sends ISO-8601 with an offset, so the browser
 * resolves it to local time without the page having to know the cluster's. */
function ago(iso) {
  if (!iso) return 'never';
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

function clock(iso) {
  return iso ? new Date(iso).toLocaleString() : '—';
}

function duration(from, to) {
  if (!from) return '—';
  const end = to ? new Date(to).getTime() : Date.now();
  const minutes = Math.max(0, (end - new Date(from).getTime()) / 60000);
  if (minutes < 60) return `${Math.round(minutes)} min`;
  return `${Math.floor(minutes / 60)}h ${Math.round(minutes % 60)}m`;
}

// ---------------------------------------------------------------------- api

async function api(path, options = {}) {
  const response = await fetch(API + path, {
    ...options,
    headers: {
      // The entire auth story until Keycloak lands. fleet-api's settings.py
      // explains why this is acceptable and what replaces it.
      'X-Fleet-User': state.userId,
      'X-Fleet-Role': state.role,
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.detail || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function liveness(ok, note) {
  const dot = el('live-dot');
  dot.className = `dot ${ok ? 'live' : 'down'}`;
  el('live-text').textContent = note;
}

/* One place that turns a thrown API error into something on screen. A 404 here
 * is usually the authorisation rule working rather than a fault, so it says
 * so — that is the whole point of the role switcher. */
function renderError(error) {
  const scoped = error.status === 404 && state.role === 'owner';
  return `<div class="error">
    <strong>${esc(error.message)}</strong>
    ${scoped
      ? `<div style="margin-top:6px">This owner cannot see that vehicle, so the API
         answers <code>404</code> rather than <code>403</code> — a 403 would confirm
         it exists. Switch to <em>admin</em> to see the whole fleet.</div>`
      : ''}
  </div>`;
}

// ------------------------------------------------------------------- charts

/* A line chart in ~30 lines of SVG, because the alternative is shipping a
 * charting library to draw two sparklines. Anything that wants real
 * time-series exploration gets the embedded Grafana panel below instead —
 * which is why Grafana is in the architecture at all (§6.7). */
function sparkline(points, { color = '#5b9dff', lo = null, hi = null } = {}) {
  const W = 300;
  const H = 110;
  const PAD = 6;

  if (!points.length) {
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
             aria-label="no data"><text x="${W / 2}" y="${H / 2}" fill="#949cb0"
             font-size="11" text-anchor="middle">no readings in this window</text></svg>`;
  }

  const values = points.map((p) => p.v);
  let min = lo === null ? Math.min(...values) : lo;
  let max = hi === null ? Math.max(...values) : hi;
  // A flat series has min === max, which would divide by zero and, worse, draw
  // a line at the top of the box rather than through the middle.
  if (max - min < 1e-9) { min -= 1; max += 1; }

  const x = (i) => PAD + (i / Math.max(1, points.length - 1)) * (W - 2 * PAD);
  const y = (v) => H - PAD - ((v - min) / (max - min)) * (H - 2 * PAD);

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
  const area = `${line}L${x(points.length - 1).toFixed(1)},${H - PAD}L${x(0).toFixed(1)},${H - PAD}Z`;
  const id = `g${Math.random().toString(36).slice(2, 8)}`;

  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
           aria-label="${points.length} points, ${num(min)} to ${num(max)}">
    <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    <path d="${area}" fill="url(#${id})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="1.6"
          stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

// -------------------------------------------------------------------- views

const STATUS_WORD = { ok: 'ok', warn: 'warn', critical: 'critical', offline: 'offline' };

function vehicleCard(v) {
  const m = v.metrics || {};
  const hot = m.coolant_temp > 110 ? 'hot' : '';
  const weak = m.module_voltage !== undefined && m.module_voltage < 13 ? 'low' : '';
  return `<a class="card ${v.status}" href="#/vehicle/${encodeURIComponent(v.vehicle_id)}">
    <div class="card-head">
      <div>
        <div class="card-id">${esc(v.vehicle_id)}</div>
        <div class="card-label">${esc(v.label || '')}</div>
      </div>
      <span class="chip ${v.status}">${STATUS_WORD[v.status]}</span>
    </div>
    <div class="readout">
      <div><span class="k">speed</span><span class="v">${num(m.speed, 0)}<small> km/h</small></span></div>
      <div><span class="k">coolant</span><span class="v ${hot}">${num(m.coolant_temp)}<small> °C</small></span></div>
      <div><span class="k">fuel</span><span class="v">${num(m.fuel_level, 0)}<small> %</small></span></div>
      <div><span class="k">voltage</span><span class="v ${weak}">${num(m.module_voltage, 1)}<small> V</small></span></div>
    </div>
    <div class="card-foot">
      <span>${ago(v.last_seen)}</span>
      <span>${v.open_alerts ? `${v.open_alerts} open alert${v.open_alerts > 1 ? 's' : ''}` : 'no alerts'}</span>
    </div>
  </a>`;
}

async function viewFleet(root) {
  const draw = async () => {
    try {
      const vehicles = await api('/vehicles');
      liveness(true, `${vehicles.length} vehicles`);
      root.innerHTML = vehicles.length
        ? `<div class="section">
             <h2>Fleet</h2>
             <div class="grid">${vehicles.map(vehicleCard).join('')}</div>
           </div>`
        : `<div class="panel"><div class="empty">No vehicles visible to this role.</div></div>`;
    } catch (error) {
      liveness(false, 'api error');
      root.innerHTML = renderError(error);
    }
  };
  await draw();
  state.timers.push(setInterval(draw, POLL_MS));
}

const GAUGES = [
  { key: 'speed', label: 'speed', unit: 'km/h', lo: 0, hi: 160, digits: 0 },
  { key: 'rpm', label: 'rpm', unit: '', lo: 0, hi: 7000, digits: 0 },
  { key: 'coolant_temp', label: 'coolant', unit: '°C', lo: 40, hi: 130, digits: 1, warnAbove: 110, critAbove: 120 },
  { key: 'fuel_level', label: 'fuel', unit: '%', lo: 0, hi: 100, digits: 0, warnBelow: 15 },
  { key: 'module_voltage', label: 'voltage', unit: 'V', lo: 11, hi: 15, digits: 2, warnBelow: 13 },
  { key: 'engine_load', label: 'load', unit: '%', lo: 0, hi: 100, digits: 0 },
];

function gauge(spec, value) {
  if (value === undefined || value === null) {
    return `<div class="gauge"><div class="gauge-k">${spec.label}</div>
      <div class="gauge-v">—</div><div class="bar"><i style="width:0"></i></div></div>`;
  }
  const pct = Math.max(0, Math.min(100, ((value - spec.lo) / (spec.hi - spec.lo)) * 100));
  let tone = 'ok';
  if (spec.critAbove !== undefined && value >= spec.critAbove) tone = 'critical';
  else if (spec.warnAbove !== undefined && value >= spec.warnAbove) tone = 'warn';
  else if (spec.warnBelow !== undefined && value <= spec.warnBelow) tone = 'warn';
  return `<div class="gauge">
    <div class="gauge-k">${spec.label}</div>
    <div class="gauge-v">${num(value, spec.digits)}<small>${spec.unit}</small></div>
    <div class="bar"><i class="${tone}" style="width:${pct.toFixed(1)}%"></i></div>
  </div>`;
}

const CHARTS = [
  { metric: 'speed', title: 'Speed', unit: 'km/h', color: '#5b9dff' },
  { metric: 'coolant_temp', title: 'Coolant temperature', unit: '°C', color: '#f0604d' },
  { metric: 'module_voltage', title: 'Control module voltage', unit: 'V', color: '#3ec98a' },
  { metric: 'rpm', title: 'Engine speed', unit: 'rpm', color: '#e8b046' },
];

async function viewVehicle(root, vehicleId) {
  const metrics = CHARTS.map((c) => `metric=${encodeURIComponent(c.metric)}`).join('&');

  const draw = async () => {
    try {
      const [vehicle, series, trips, alerts] = await Promise.all([
        api(`/vehicles/${encodeURIComponent(vehicleId)}`),
        api(`/vehicles/${encodeURIComponent(vehicleId)}/series?hours=1&${metrics}`),
        api(`/vehicles/${encodeURIComponent(vehicleId)}/trips?limit=10`),
        api(`/alerts?vehicle_id=${encodeURIComponent(vehicleId)}&only_open=false&limit=20`),
      ]);
      liveness(true, `${esc(vehicleId)} ${ago(vehicle.last_seen)}`);

      const m = vehicle.metrics || {};
      // `coolant_temp` is charted on max rather than avg: averaging a 60-second
      // bucket flattens exactly the spike the overheat alert fires on, so the
      // chart would disagree with the alert beside it.
      const pick = (metric) => (metric === 'coolant_temp' ? 'max' : 'avg');

      root.innerHTML = `
        <div class="detail-head">
          <h1>${esc(vehicle.vehicle_id)}</h1>
          <span class="chip ${vehicle.status}">${STATUS_WORD[vehicle.status]}</span>
        </div>
        <div class="detail-sub">
          ${esc(vehicle.label || 'no label')} &middot; last reading ${ago(vehicle.last_seen)}
          &middot; owner <span style="font-family:var(--mono)">${esc(vehicle.owner_id)}</span>
          ${vehicle.revoked_at ? ' &middot; <strong style="color:var(--critical)">certificate revoked</strong>' : ''}
          &middot; <a href="#/fleet">back to fleet</a>
        </div>

        <div class="section gauges">
          ${GAUGES.map((g) => gauge(g, m[g.key])).join('')}
        </div>

        <div class="section">
          <h2>Last hour &middot; ${series.bucket_seconds}s buckets</h2>
          <div class="charts">
            ${CHARTS.map((c) => {
              const points = (series.series[c.metric] || []).map((p) => ({ v: p[pick(c.metric)] }));
              const values = points.map((p) => p.v);
              const range = values.length
                ? `${num(Math.min(...values))} – ${num(Math.max(...values))} ${c.unit}`
                : 'no data';
              return `<div class="chart">
                <h3>${c.title}</h3>
                <div class="range">${range}${c.metric === 'coolant_temp' ? ' (bucket max)' : ''}</div>
                ${sparkline(points, { color: c.color })}
              </div>`;
            }).join('')}
          </div>
        </div>

        <div class="section">
          <h2>Trips</h2>
          <div class="panel">${trips.length ? `
            <table>
              <thead><tr>
                <th>Started</th><th>Duration</th>
                <th style="text-align:right">Distance</th>
                <th style="text-align:right">Max speed</th>
                <th style="text-align:right">Max coolant</th>
              </tr></thead>
              <tbody>${trips.map((t) => `<tr>
                <td>${clock(t.started_at)}</td>
                <td>${duration(t.started_at, t.ended_at)}${t.ended_at ? '' : ' <em>(open)</em>'}</td>
                <td class="num">${num(t.distance_km, 2)} km</td>
                <td class="num">${num(t.max_speed, 0)} km/h</td>
                <td class="num">${num(t.max_coolant, 1)} °C</td>
              </tr>`).join('')}</tbody>
            </table>` : '<div class="empty">No trips recorded yet — the rollup runs once a minute.</div>'}
          </div>
        </div>

        <div class="section">
          <h2>Alerts</h2>
          <div class="panel">${alerts.length ? alertTable(alerts) :
            '<div class="empty">No alerts for this vehicle.</div>'}</div>
        </div>

        <div class="section">
          <h2>Grafana</h2>
          <!-- Embedded rather than reimplemented (§6.7). Needs
               GF_SECURITY_ALLOW_EMBEDDING, which grafana.yaml already sets;
               without it this frame renders Grafana's refusal page. -->
          <iframe class="grafana" title="Grafana fleet overview"
            src="/grafana/d/fc-fleet-overview?kiosk&theme=dark&from=now-1h&to=now&var-vehicle=${encodeURIComponent(vehicleId)}"></iframe>
        </div>`;
      wireAck(root, draw);
    } catch (error) {
      liveness(false, 'api error');
      root.innerHTML = renderError(error) + '<p><a href="#/fleet">back to fleet</a></p>';
    }
  };

  await draw();
  state.timers.push(setInterval(draw, SERIES_POLL_MS));
}

function alertTable(alerts) {
  return `<table>
    <thead><tr>
      <th>Vehicle</th><th>Kind</th><th>Severity</th><th>Opened</th><th>State</th><th></th>
    </tr></thead>
    <tbody>${alerts.map((a) => `<tr>
      <td class="mono"><a href="#/vehicle/${encodeURIComponent(a.vehicle_id)}">${esc(a.vehicle_id)}</a></td>
      <td>${esc(a.kind)}</td>
      <td class="sev ${esc(a.severity)}">${esc(a.severity)}</td>
      <td title="${esc(clock(a.opened_at))}">${ago(a.opened_at)}</td>
      <td>${a.closed_at ? `closed ${ago(a.closed_at)}` : (a.acked_at ? `acked ${ago(a.acked_at)}` : 'open')}</td>
      <td style="text-align:right">${a.acked_at
        ? '<span style="color:var(--fg-dim);font-size:12px">acknowledged</span>'
        : `<button data-ack="${a.alert_id}">Acknowledge</button>`}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

/* Acknowledge is the one write this UI makes. Optimism is deliberately absent:
 * the button disables, the POST completes, and the view redraws from the API —
 * so what the page shows is always what the database says. */
function wireAck(root, redraw) {
  root.querySelectorAll('button[data-ack]').forEach((button) => {
    button.addEventListener('click', async (event) => {
      event.preventDefault();
      button.disabled = true;
      button.textContent = 'Acknowledging…';
      try {
        await api(`/alerts/${button.dataset.ack}/ack`, { method: 'POST' });
        await redraw();
        refreshSummary();
      } catch (error) {
        button.disabled = false;
        button.textContent = 'Acknowledge';
        liveness(false, error.message);
      }
    });
  });
}

async function viewAlerts(root) {
  const draw = async () => {
    try {
      const [open, recent] = await Promise.all([
        api('/alerts?only_open=true&limit=200'),
        api('/alerts?only_open=false&limit=50'),
      ]);
      liveness(true, `${open.length} open`);
      const closed = recent.filter((a) => a.closed_at);
      root.innerHTML = `
        <div class="section">
          <h2>Open alerts</h2>
          <div class="panel">${open.length ? alertTable(open) :
            '<div class="empty">Nothing open. Inject a fault with <code>./fc.sh fault overheat</code> to see this fill.</div>'}</div>
        </div>
        <div class="section">
          <h2>Recently closed</h2>
          <div class="panel">${closed.length ? alertTable(closed) :
            '<div class="empty">Nothing closed yet — the processor auto-closes an alert once its condition has been absent for five minutes.</div>'}</div>
        </div>`;
      wireAck(root, draw);
    } catch (error) {
      liveness(false, 'api error');
      root.innerHTML = renderError(error);
    }
  };
  await draw();
  state.timers.push(setInterval(draw, POLL_MS));
}

// ------------------------------------------------------------------- router

async function refreshSummary() {
  try {
    const s = await api('/summary');
    el('s-online').textContent = `${s.online}/${s.vehicles}`;
    el('s-alerts').textContent = s.open_alerts;
    el('s-distance').textContent = num(s.distance_today_km, 1);
    el('s-rate').textContent = s.readings_last_minute;

    const badge = el('nav-alert-count');
    badge.textContent = s.unacked_alerts;
    badge.classList.toggle('hidden', !s.unacked_alerts);

    el('foot-note').textContent = `${s.role} · ${s.user_id}`;
  } catch (error) {
    liveness(false, error.message);
  }
}

function clearTimers() {
  state.timers.forEach(clearInterval);
  state.timers = [];
}

async function route() {
  clearTimers();
  const root = el('view');
  const hash = location.hash || '#/fleet';
  const [, view, arg] = hash.split('/');

  document.querySelectorAll('#nav a[data-view]').forEach((a) => {
    a.classList.toggle('active', a.dataset.view === (view || 'fleet'));
  });

  root.innerHTML = '<div class="empty">Loading…</div>';
  if (view === 'vehicle' && arg) await viewVehicle(root, decodeURIComponent(arg));
  else if (view === 'alerts') await viewAlerts(root);
  else await viewFleet(root);

  refreshSummary();
  state.timers.push(setInterval(refreshSummary, POLL_MS));
}

el('role').addEventListener('change', (event) => {
  const [role, userId] = event.target.value.split(':');
  state.role = role;
  // The user id only matters for `owner`; admin and operator see the whole
  // fleet regardless, so the default is kept for the ack audit trail.
  if (userId) state.userId = userId;
  route();
});

window.addEventListener('hashchange', route);
route();
