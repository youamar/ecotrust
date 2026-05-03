const fmt = (n, d = 1) => Number(n).toLocaleString(undefined, { maximumFractionDigits: d });
const tickIcon = "✓"; const xIcon = "✗";

let powerChart;

async function refreshAll() {
  const [ghg, peak, occ, lb, daily, audit, notifs, tariff] = await Promise.all([
    fetch("/ghg/scope2").then(r => r.json()),
    fetch("/peak/savings").then(r => r.json()),
    fetch("/occupancy/latest").then(r => r.json()),
    fetch("/departments/leaderboard").then(r => r.json()),
    fetch("/consumption/daily").then(r => r.json()),
    fetch("/audit?limit=30").then(r => r.json()),
    fetch("/notifications").then(r => r.json()),
    fetch("/tariff/projection").then(r => r.json()),
  ]);
  renderKPIs(ghg, peak, occ);
  renderTariff(tariff);
  renderFloorplan(occ);
  renderHeatmap(occ);
  renderLeaderboard(lb);
  renderGHG(ghg);
  renderChart(daily);
  renderAudit(audit);
  renderNotifs(notifs);
}

function renderTariff(t) {
  const a = t.actual, b = t.baseline;
  document.getElementById("tariff-actual").textContent = `RM ${fmt(a.total_rm, 0)}`;
  document.getElementById("tariff-baseline").textContent = `RM ${fmt(b.total_rm, 0)}`;
  document.getElementById("tariff-savings").textContent = `RM ${fmt(t.savings_rm, 0)} saved`;
  document.getElementById("tariff-pct").textContent = `${fmt(t.savings_pct, 1)} % vs baseline`;
  document.getElementById("tariff-energy").textContent = `RM ${fmt(a.energy_rm, 0)}`;
  document.getElementById("tariff-md").textContent = `RM ${fmt(a.md_rm, 0)}`;
  document.getElementById("tariff-cap").textContent = `RM ${fmt(a.capacity_rm, 0)}`;
}

function renderKPIs(ghg, peak, occ) {
  document.getElementById("kpi-avoided").textContent = `${fmt(ghg.avoided_kg)} kg`;
  document.getElementById("kpi-scope2").textContent = `${fmt(ghg.scope2_kg)} kg`;
  document.getElementById("kpi-kwh").textContent =
    `${fmt(ghg.actual_kwh)} kWh consumed (baseline ${fmt(ghg.baseline_kwh)})`;
  document.getElementById("kpi-md").textContent = `RM ${fmt(peak.estimated_md_savings_rm_per_month, 0)}/mo`;
  document.getElementById("kpi-peak").textContent =
    `${fmt(peak.peak_actual_kw)} kW peak vs ${fmt(peak.peak_baseline_kw)} kW baseline · cap ${fmt(peak.soft_cap_kw)} kW`;
  const granted = occ.filter(r => r.granted).length;
  document.getElementById("kpi-rooms").textContent = occ.length;
  document.getElementById("kpi-grants").textContent = `${granted} granted now`;
}

function renderFloorplan(occ) {
  const svg = document.getElementById("floorplan");
  const W = 900, FH = 110, MARGIN = 30;
  // Group by floor and sort; render top-down (top floor first).
  const byFloor = {};
  occ.forEach(r => { (byFloor[r.floor] = byFloor[r.floor] || []).push(r); });
  const floors = Object.keys(byFloor).map(Number).sort((a,b) => b - a);
  const H = floors.length * FH + 30;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const colorFor = r => {
    if (r.override) return "#f59e0b";
    if (!r.granted) return "#475569";
    const i = Math.min(1, r.headcount / 6);
    const r2 = Math.round(16 + (110 - 16) * i);
    const g2 = Math.round(185 + (231 - 185) * (1 - i));
    const b2 = Math.round(129);
    return `rgb(${r2},${g2},${b2})`;
  };

  let svgContent = "";
  floors.forEach((floor, fi) => {
    const y = fi * FH + 10;
    const rooms = byFloor[floor];
    const roomW = (W - MARGIN * 2) / rooms.length - 8;

    // Floor slab
    svgContent += `
      <rect x="${MARGIN-10}" y="${y}" width="${W-MARGIN*2+20}" height="${FH-20}"
            fill="#0f172a" stroke="#1e293b" rx="6"/>
      <text x="${MARGIN-5}" y="${y+18}" fill="#64748b" font-size="11" font-family="ui-sans-serif">Floor ${floor}</text>`;

    rooms.forEach((r, i) => {
      const x = MARGIN + i * (roomW + 8);
      const ry = y + 26;
      const rh = FH - 50;
      const fill = colorFor(r);
      svgContent += `
        <a href="/room/${r.room_id}" style="cursor:pointer">
          <rect x="${x}" y="${ry}" width="${roomW}" height="${rh}" rx="6"
                fill="${fill}" fill-opacity="0.55" stroke="${fill}" stroke-width="1.5">
            <title>${r.name} · ${r.department} · ${r.tier}
${r.headcount} occupant(s) · ${r.applied_kw.toFixed(2)} kW
${r.reason}</title>
          </rect>
          <text x="${x+8}" y="${ry+18}" fill="#e2e8f0" font-size="12" font-weight="600">${r.name}</text>
          <text x="${x+8}" y="${ry+34}" fill="#cbd5e1" font-size="11">👥 ${r.headcount} · ${r.applied_kw.toFixed(1)} kW</text>
          <text x="${x+roomW-6}" y="${ry+18}" fill="#0b1220" font-size="10" font-weight="700"
                text-anchor="end">${r.override ? 'OVR' : (r.granted ? 'ON' : 'OFF')}</text>
        </a>`;
    });
  });
  svg.innerHTML = svgContent;
}

function renderHeatmap(occ) {
  const heat = document.getElementById("heatmap");
  heat.innerHTML = occ.map(r => {
    const intensity = Math.min(1, r.headcount / 5);
    const bg = r.granted
      ? `rgba(16,185,129,${0.18 + intensity * 0.55})`
      : `rgba(100,116,139,0.15)`;
    let pillCls = r.granted ? "grant" : "deny";
    let pillTxt = r.granted ? "GRANT" : "DENY";
    if (r.override) { pillCls = "over"; pillTxt = "OVERRIDE"; }
    return `
      <div class="heat p-3 rounded-lg border border-slate-700" style="background:${bg}">
        <div class="flex justify-between items-start">
          <a href="/room/${r.room_id}" class="hover:underline">
            <div class="font-semibold">${r.name}</div>
            <div class="text-xs text-slate-400">F${r.floor} · ${r.department} · ${r.tier}</div>
          </a>
          <span class="pill ${pillCls}">${pillTxt}</span>
        </div>
        <div class="mt-2 text-sm">
          👥 <b>${r.headcount}</b> · ${fmt(r.applied_kw, 2)} kW
          <span class="text-slate-500"> / ${fmt(r.baseline_kw, 2)} baseline</span>
        </div>
        <div class="text-xs text-slate-500 mt-1 truncate" title="${r.reason}">${r.reason}</div>
        <div class="mt-2 flex gap-2">
          <button class="text-xs px-2 py-1 rounded border border-slate-600 hover:bg-slate-700"
                  onclick="overrideRoom(${r.room_id}, ${!r.granted})">
            ${r.granted ? 'Force deny' : 'Force grant'} 4h
          </button>
          ${r.override ? `<button class="text-xs px-2 py-1 rounded border border-amber-500 text-amber-400 hover:bg-amber-900/30"
            onclick="clearOverride(${r.room_id})">clear override</button>` : ""}
        </div>
      </div>`;
  }).join("");
}

function renderLeaderboard(lb) {
  document.getElementById("lb-body").innerHTML = lb.map((d, i) => `
    <tr class="border-t border-slate-800">
      <td class="py-1 text-slate-500">${i + 1}</td>
      <td class="py-1">${d.department}</td>
      <td class="py-1 text-right text-emerald-400 font-semibold">${fmt(d.score, 1)}%</td>
    </tr>`).join("");
}

function renderGHG(ghg) {
  document.getElementById("ghg-tbl").querySelector("tbody").innerHTML = `
    <tr><td class="py-1 text-slate-400">Electricity Consumption</td><td class="py-1 text-right">${fmt(ghg.actual_kwh)} kWh</td></tr>
    <tr><td class="py-1 text-slate-400">Baseline (without EcoTrust)</td><td class="py-1 text-right">${fmt(ghg.baseline_kwh)} kWh</td></tr>
    <tr><td class="py-1 text-slate-400">Avoided Consumption</td><td class="py-1 text-right text-emerald-400">${fmt(ghg.avoided_kwh)} kWh</td></tr>
    <tr><td class="py-1 text-slate-400">Emission Factor (TNB 2024)</td><td class="py-1 text-right">${ghg.emission_factor}</td></tr>
    <tr class="border-t border-slate-700"><td class="py-2 font-semibold">Total Scope 2 Emissions</td><td class="py-2 text-right font-semibold">${fmt(ghg.scope2_kg)} kgCO₂e</td></tr>
    <tr><td class="py-1 text-emerald-400 font-semibold">Avoided Emissions</td><td class="py-1 text-right text-emerald-400 font-semibold">${fmt(ghg.avoided_kg)} kgCO₂e</td></tr>`;
}

function renderChart(daily) {
  const labels = daily.map(d => new Date(d.hour).getHours() + ":00");
  const actual = daily.map(d => d.actual_kw_total);
  const baseline = daily.map(d => d.baseline_kw_total);
  if (!powerChart) {
    powerChart = new Chart(document.getElementById("powerChart"), {
      type: "line",
      data: { labels, datasets: [
        { label: "Baseline kW", data: baseline, borderColor: "#94a3b8",
          backgroundColor: "rgba(148,163,184,0.15)", fill: true, tension: 0.3 },
        { label: "Actual kW (EcoTrust)", data: actual, borderColor: "#10b981",
          backgroundColor: "rgba(16,185,129,0.25)", fill: true, tension: 0.3 },
      ]},
      options: {
        plugins: { legend: { labels: { color: "#cbd5e1" } } },
        scales: {
          x: { ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } },
          y: { ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } },
        },
      },
    });
  } else {
    powerChart.data.labels = labels;
    powerChart.data.datasets[0].data = baseline;
    powerChart.data.datasets[1].data = actual;
    powerChart.update("none");
  }
}

function renderAudit(audit) {
  document.getElementById("audit-body").innerHTML = audit.map(a => {
    const t = new Date(a.timestamp).toLocaleTimeString();
    const verdictCls = a.granted ? "text-emerald-400" : "text-rose-400";
    const ck = ok => `<span class="${ok ? 'text-emerald-400' : 'text-rose-400'}">${ok ? tickIcon : xIcon}</span>`;
    return `
      <tr class="border-b border-slate-800 hover:bg-slate-800/40">
        <td class="py-1 text-slate-400">${t}</td>
        <td class="py-1">${a.room_name} <span class="text-slate-500 text-xs">(${a.headcount}👥)</span></td>
        <td class="py-1 text-center">${ck(a.identity_ok)}</td>
        <td class="py-1 text-center">${ck(a.presence_ok)}</td>
        <td class="py-1 text-center">${ck(a.context_ok)}</td>
        <td class="py-1 text-right">${fmt(a.applied_kw, 2)}</td>
        <td class="py-1 ${verdictCls} text-xs">${a.reason}</td>
      </tr>`;
  }).join("");
}

function renderNotifs(notifs) {
  document.getElementById("notif-count").textContent = `${notifs.length} open`;
  const list = document.getElementById("notif-list");
  if (!notifs.length) {
    list.innerHTML = `<div class="text-sm text-slate-500">No open advisories.</div>`;
    return;
  }
  list.innerHTML = notifs.map(n => `
    <div class="flex justify-between items-start gap-3 p-2 rounded border border-slate-700">
      <div>
        <div class="text-xs sev-${n.severity} uppercase font-semibold">${n.severity}</div>
        <div class="text-sm">${n.message}</div>
        <div class="text-xs text-slate-500">${new Date(n.created_at).toLocaleString()}</div>
      </div>
      <button class="text-xs px-2 py-1 border border-slate-600 rounded hover:bg-slate-700"
              onclick="ackNotif(${n.id})">Ack</button>
    </div>`).join("");
}

async function overrideRoom(roomId, grant) {
  const reason = prompt(`${grant ? 'GRANT' : 'DENY'} room ${roomId} — reason?`, grant ? "manual override" : "facility check");
  if (reason === null) return;
  await fetch(`/override/${roomId}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ granted: grant, ttl_seconds: 4 * 3600, reason }),
  });
  refreshAll();
}

async function clearOverride(roomId) {
  await fetch(`/override/${roomId}`, { method: "DELETE" });
  refreshAll();
}

async function ackNotif(nid) {
  await fetch(`/notifications/${nid}/ack`, { method: "POST" });
  fetch("/notifications").then(r => r.json()).then(renderNotifs);
}

// SSE live updates
function connectSSE() {
  const es = new EventSource("/events");
  const status = document.getElementById("live-status");
  es.addEventListener("hello", () => status.textContent = "live");
  es.addEventListener("decision", () => refreshAll());
  es.addEventListener("override", () => refreshAll());
  es.addEventListener("override_cleared", () => refreshAll());
  es.onerror = () => {
    status.textContent = "reconnecting…";
    setTimeout(connectSSE, 2000);
    es.close();
  };
}

refreshAll();
connectSSE();
// Safety net poll every 30s in case SSE dies silently
setInterval(refreshAll, 30000);
