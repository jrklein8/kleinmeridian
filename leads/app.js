/* JEM Roofing Lead Dashboard - front-end */

const state = {
  window: "6m",
  radius: 75,
  unit: "bg",
  types: { hail: true, wind: true, tornado: true },
  layers: { zips: true, swaths: false, radar: false, heat: false, pins: true },
  topMode: "areas",
};

const TYPE_COLORS = { hail: "#25c3d6", wind: "#b07bff", tornado: "#ff4d6d" };
const CENTER = [34.2257, -77.9447];

const map = L.map("map", { zoomControl: true }).setView(CENTER, 9);
L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  attribution: "&copy; OpenStreetMap &copy; CARTO",
  subdomains: "abcd",
  maxZoom: 19,
}).addTo(map);

// Stacking order via panes (low -> high)
const PANES = { heat: 410, zips: 420, swaths: 430, radar: 440, pins: 450, ring: 460 };
for (const [name, z] of Object.entries(PANES)) {
  map.createPane(name);
  map.getPane(name).style.zIndex = z;
}
const heatRenderer = L.canvas({ pane: "heat" });
const radarRenderer = L.canvas({ pane: "radar" });

const heatLayer = L.layerGroup();
const zipLayer = L.layerGroup();
const swathLayer = L.layerGroup();
const radarLayer = L.layerGroup();
const pinLayer = L.layerGroup();
const overlayLayer = L.layerGroup().addTo(map);

// Damage-grade legend as a collapsible map control (bottom-left of the map)
const LEGEND_KEY = "jem.legend.collapsed";
const legendControl = L.control({ position: "bottomleft" });
legendControl.onAdd = function () {
  const div = L.DomUtil.create("div", "map-legend");
  if (localStorage.getItem(LEGEND_KEY) === "1") div.classList.add("collapsed");
  div.innerHTML =
    `<div class="map-legend-head"><span class="map-legend-title">Damage grade</span><span class="chevron"></span></div>` +
    `<div class="map-legend-body"><div class="legend" id="legend"></div><div class="legend radar-legend" id="radar-legend"></div></div>`;
  L.DomEvent.disableClickPropagation(div);
  L.DomEvent.disableScrollPropagation(div);
  div.querySelector(".map-legend-head").addEventListener("click", () => {
    div.classList.toggle("collapsed");
    try { localStorage.setItem(LEGEND_KEY, div.classList.contains("collapsed") ? "1" : "0"); } catch (e) { /* ignore */ }
  });
  return div;
};
legendControl.addTo(map);

let latest = null;

// --------------------------------------------------------------------------- //
// Controls
// --------------------------------------------------------------------------- //

function segHandler(id, key, after) {
  document.getElementById(id).addEventListener("click", (e) => {
    const btn = e.target.closest(`button[data-${key}]`);
    if (!btn) return;
    document.querySelectorAll(`#${id} button`).forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    after(btn.dataset[key]);
  });
}

segHandler("window-seg", "window", (v) => { state.window = v; load(); });
segHandler("unit-seg", "unit", (v) => { state.unit = v; load(); });
segHandler("top-mode", "mode", (v) => { state.topMode = v; if (latest) renderTopList(); });

const radiusInput = document.getElementById("radius");
const radiusOut = document.getElementById("radius-out");
radiusInput.addEventListener("input", () => {
  state.radius = +radiusInput.value;
  radiusOut.textContent = `${state.radius} mi`;
  drawRadius();
});
radiusInput.addEventListener("change", () => applyView());

document.querySelectorAll(".checks input[data-type]").forEach((cb) => {
  cb.addEventListener("change", () => { state.types[cb.dataset.type] = cb.checked; applyView(); });
});

document.querySelectorAll(".layers input[data-layer]").forEach((cb) => {
  cb.addEventListener("change", () => {
    state.layers[cb.dataset.layer] = cb.checked;
    applyLayerVisibility();
    if (cb.dataset.layer === "radar" && raw) renderLegend(raw.grade_bands, raw.radar_legend);
  });
});

document.getElementById("export-csv").addEventListener("click", exportCSV);
document.getElementById("export-geojson").addEventListener("click", exportGeoJSON);

function downloadBlob(filename, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}
function csvCell(v) {
  v = v == null ? "" : String(v);
  return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
}
function exportCSV() {
  if (!latest) return;
  const cols = ["name", "town", "grade", "score", "reports", "warnings",
    "max_hail_in", "max_wind_mph", "radar_hail_in", "last", "lat", "lon", "id"];
  const rows = latest.neighborhoods.features.map((f) => f.properties).sort((a, b) => b.score - a.score);
  const csv = [cols.join(",")].concat(rows.map((p) => cols.map((c) => csvCell(p[c])).join(","))).join("\n");
  downloadBlob(`jem_targets_${state.unit}_${state.window}.csv`, csv, "text/csv");
}
function exportGeoJSON() {
  if (!latest) return;
  downloadBlob(`jem_targets_${state.unit}_${state.window}.geojson`,
    JSON.stringify(latest.neighborhoods), "application/geo+json");
}

// --------------------------------------------------------------------------- //
// Map drawing
// --------------------------------------------------------------------------- //

function drawRadius() {
  overlayLayer.clearLayers();
  L.circleMarker(CENTER, {
    pane: "ring", radius: 6, color: "#fff", weight: 2, fillColor: "#2f81f7", fillOpacity: 1,
  }).bindTooltip("Wilmington, NC", { direction: "top" }).addTo(overlayLayer);
  L.circle(CENTER, {
    pane: "ring", radius: state.radius * 1609.34,
    color: "#7fa8d0", weight: 1.5, dashArray: "6 6", fill: false,
  }).addTo(overlayLayer);
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

let nbLayers = {};   // area id -> Leaflet layer, so the list can open its popup

function drawNeighborhoods(fc) {
  zipLayer.clearLayers();
  nbLayers = {};
  L.geoJSON(fc, {
    pane: "zips",
    style: (f) => ({
      pane: "zips", color: "#0b1118", weight: 0.6,
      fillColor: f.properties.color, fillOpacity: 0.55,
    }),
    onEachFeature: (f, lyr) => {
      const p = f.properties;
      nbLayers[p.id] = lyr;
      const bits = [];
      if (p.max_hail_in) bits.push(`${p.max_hail_in}" hail`);
      if (p.max_wind_mph) bits.push(`${p.max_wind_mph} mph wind`);
      if (p.radar_hail_in) bits.push(`radar ${p.radar_hail_in}"`);
      lyr.bindPopup(
        `<div class="pop-title">${p.name} &mdash; ${p.grade}</div>` +
        (p.town && !p.name.includes(p.town) ? `<div class="pop-meta">near ${p.town}</div>` : "") +
        `<div>${bits.join(" &middot; ") || "severe storm coverage"}</div>` +
        `<div class="pop-meta">${p.reports} report${p.reports !== 1 ? "s" : ""} &middot; ${p.warnings} warning${p.warnings !== 1 ? "s" : ""} &middot; score ${p.score}</div>` +
        `<div class="pop-meta">last: ${fmtDate(p.last)}</div>` +
        `<a class="pop-link" href="#" onclick="exportAddresses('${p.id}');return false;">⤓ Export street addresses (CSV)</a>`,
        { autoPan: false }
      );
      lyr.on("mouseover", () => lyr.setStyle({ weight: 2, color: "#fff" }));
      lyr.on("mouseout", () => lyr.setStyle({ weight: 0.6, color: "#0b1118" }));
    },
  }).addTo(zipLayer);
}

function focusArea(id, lat, lon) {
  const lyr = nbLayers[id];
  if (!lyr) {                                  // town rows / missing polygon -> just fly
    map.flyTo([lat, lon], state.unit === "bg" ? 12 : 11, { duration: 0.6 });
    return;
  }
  if (!map.hasLayer(zipLayer)) zipLayer.addTo(map);   // popup needs the layer visible
  map.flyToBounds(lyr.getBounds(), { maxZoom: state.unit === "bg" ? 14 : 12, duration: 0.6 });
  let opened = false;
  const open = () => { if (!opened) { opened = true; lyr.openPopup(); } };
  map.once("moveend", open);
  setTimeout(open, 750);                         // fallback if the view didn't move
}

function drawSwaths(fc) {
  swathLayer.clearLayers();
  L.geoJSON(fc, {
    pane: "swaths",
    style: (f) => ({
      pane: "swaths", color: f.properties.color, weight: 1.2,
      fillColor: f.properties.color, fillOpacity: 0.18, dashArray: "4 3",
    }),
    onEachFeature: (f, lyr) => {
      const p = f.properties;
      const bits = [];
      if (p.hail_in) bits.push(`${p.hail_in}" hail`);
      if (p.wind_mph) bits.push(`${p.wind_mph} mph wind`);
      if (p.damagetag) bits.push(`${p.damagetag.toLowerCase()} damage`);
      lyr.bindPopup(
        `<div class="pop-title">${p.ps}</div><div>${bits.join(" &middot; ") || "severe storm"}</div>` +
        `<div class="pop-meta">${fmtDate(p.issue)}</div>`
      );
    },
  }).addTo(swathLayer);
}

function drawRadar(fc) {
  radarLayer.clearLayers();
  L.geoJSON(fc, {
    renderer: radarRenderer, pane: "radar",
    style: (f) => ({ stroke: false, fillColor: f.properties.color, fillOpacity: 0.7 }),
    interactive: false,
  }).addTo(radarLayer);
}

function drawHeat(cells) {
  heatLayer.clearLayers();
  for (const cell of cells) {
    const [s, w, n, e] = cell.bounds;
    L.rectangle([[s, w], [n, e]], {
      renderer: heatRenderer, pane: "heat",
      stroke: false, fillColor: cell.color, fillOpacity: 0.5, interactive: false,
    }).addTo(heatLayer);
  }
}

function drawPins(reports) {
  pinLayer.clearLayers();
  for (const r of reports) {
    const color = TYPE_COLORS[r.cat] || "#fff";
    const mag = r.cat === "hail" ? `${r.hail_in || "?"}" hail`
      : r.cat === "wind" ? `${r.wind_mph || "?"} mph wind` : "tornado";
    const radius = 4 + Math.min(8, (r.weight || 1) * 0.7);
    L.circleMarker([r.lat, r.lon], {
      pane: "pins", radius, color: "#0b1118", weight: 1, fillColor: color, fillOpacity: 0.9,
    }).bindPopup(
      `<div class="pop-title">${r.typetext}</div><div>${mag}</div>` +
      `<div class="pop-meta">${r.city ? r.city + ", " : ""}${r.state} &middot; ${fmtDate(r.valid)}</div>` +
      (r.remark ? `<div class="pop-meta">${r.remark}</div>` : "")
    ).addTo(pinLayer);
  }
}

function applyLayerVisibility() {
  const groups = { heat: heatLayer, zips: zipLayer, swaths: swathLayer, radar: radarLayer, pins: pinLayer };
  for (const [name, lyr] of Object.entries(groups)) {
    if (state.layers[name]) lyr.addTo(map);
    else map.removeLayer(lyr);
  }
  // overlay (center + radius) lives in the high-z "ring" pane, always on top
}

// --------------------------------------------------------------------------- //
// Sidebar rendering
// --------------------------------------------------------------------------- //

function renderLegend(bands, radarLegend) {
  document.getElementById("legend").innerHTML = bands
    .map((b) => `<div class="legend-row"><span class="legend-swatch" style="background:${b.color}"></span>${b.label}</div>`)
    .join("");
  const rl = document.getElementById("radar-legend");
  if (state.layers.radar && radarLegend) {
    rl.classList.remove("hidden");
    rl.innerHTML = `<div class="legend-cap">Radar hail (MRMS)</div>` + radarLegend
      .map((b) => `<div class="legend-row"><span class="legend-swatch" style="background:${b.color}"></span>${b.label}</div>`)
      .join("");
  } else {
    rl.classList.add("hidden");
    rl.innerHTML = "";
  }
}

function renderTopList() {
  const el = document.getElementById("areas");
  const isAreas = state.topMode === "areas";
  const rows = isAreas ? latest.top_neighborhoods : latest.top_areas;

  if (!rows || !rows.length) {
    el.innerHTML = `<li class="empty">No qualifying storm activity in this window &amp; radius. Try a longer time window — quiet weather means no fresh roof leads, which is expected.</li>`;
    return;
  }

  el.innerHTML = rows.map((a, i) => {
    const bits = [];
    if (a.max_hail_in) bits.push(`${a.max_hail_in}" hail`);
    if (a.max_wind_mph) bits.push(`${a.max_wind_mph} mph`);
    let name, meta;
    if (isAreas) {
      name = a.name;
      const counts = `${a.reports} rpt${a.reports !== 1 ? "s" : ""} · ${a.warnings} warn${a.warnings !== 1 ? "s" : ""}`;
      meta = `${counts}${bits.length ? " · " + bits.join(" · ") : ""} · ${fmtDate(a.last)}`;
    } else {
      if (a.tornado) bits.push("tornado");
      name = `${a.city}${a.state ? ", " + a.state : ""}`;
      meta = `${a.count} report${a.count !== 1 ? "s" : ""}${bits.length ? " · " + bits.join(" · ") : ""} · ${fmtDate(a.last)}`;
    }
    return `<li data-lat="${a.lat}" data-lon="${a.lon}" data-id="${isAreas ? a.id : ""}">
      <span class="area-rank">${i + 1}</span>
      <span class="area-grade" style="background:${a.color}" title="${a.grade}"></span>
      <span class="area-body">
        <span class="area-name">${name}</span>
        <span class="area-meta">${meta}</span>
      </span>
      <span class="area-score">${a.score}</span>
    </li>`;
  }).join("");

  el.querySelectorAll("li[data-lat]").forEach((li) => {
    li.addEventListener("click", () => {
      focusArea(li.dataset.id, +li.dataset.lat, +li.dataset.lon);
    });
  });
}

function showBanner(msg) {
  let b = document.getElementById("banner");
  if (!b) {
    b = document.createElement("div");
    b.id = "banner";
    b.className = "banner";
    document.getElementById("app").appendChild(b);
  }
  if (!msg) { b.classList.add("hidden"); return; }
  b.textContent = msg;
  b.classList.remove("hidden");
}

// --------------------------------------------------------------------------- //
// Static data loading + client-side filtering
// --------------------------------------------------------------------------- //

let raw = null;            // merged base + unit data for the current window/unit
let manifest = null;
const dataCache = {};

function fetchJSON(path) {
  if (!dataCache[path]) {
    dataCache[path] = fetch(path).then((r) => { if (!r.ok) throw new Error(path); return r.json(); });
  }
  return dataCache[path];
}

function haversineMi(la1, lo1, la2, lo2) {
  const r = 3958.7613;
  const p1 = la1 * Math.PI / 180, p2 = la2 * Math.PI / 180;
  const dp = (la2 - la1) * Math.PI / 180, dl = (lo2 - lo1) * Math.PI / 180;
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.min(1, Math.sqrt(a)));
}
function featCenter(f) {                  // rough [lat, lon] centroid for radius filtering
  const g = f.geometry; let ring;
  if (g.type === "Polygon") ring = g.coordinates[0];
  else if (g.type === "MultiPolygon") ring = g.coordinates[0][0];
  else if (g.type === "Point") return [g.coordinates[1], g.coordinates[0]];
  else return [0, 0];
  let x = 0, y = 0;
  for (const c of ring) { x += c[0]; y += c[1]; }
  return [y / ring.length, x / ring.length];
}
function warnTypeOk(ph) {
  if (ph === "TO") return state.types.tornado;
  if (ph === "SV") return state.types.hail || state.types.wind;
  return true;
}

let reqId = 0;
async function load() {
  const myReq = ++reqId;
  const loading = document.getElementById("loading");
  loading.classList.remove("hidden");
  try {
    if (!manifest) manifest = await fetchJSON(`data/manifest.json?cb=${Date.now()}`);
    const v = manifest.generated ? `?v=${encodeURIComponent(manifest.generated)}` : "";
    const [base, unitData] = await Promise.all([
      fetchJSON(`data/${state.window}.base.json${v}`),
      fetchJSON(`data/${state.window}.${state.unit}.json${v}`),
    ]);
    if (myReq !== reqId) return;
    raw = { ...base, ...unitData, stats: { ...base.stats, neighborhoods: unitData.neighborhoods_count } };
    applyView();
    showBanner(base.data_error ? "Some sources were stale at the last refresh — showing the latest available." : "");
  } catch (err) {
    if (myReq !== reqId) return;
    showBanner("Couldn't load the storm data — it may still be generating. Try again shortly.");
    console.error(err);
  } finally {
    if (myReq === reqId) loading.classList.add("hidden");
  }
}

// Filter the loaded data by the chosen radius + event types, then draw everything.
function applyView() {
  if (!raw) return;
  const R = state.radius;
  const inR = (lat, lon) => haversineMi(CENTER[0], CENTER[1], lat, lon) <= R;

  const nbFeats = raw.neighborhoods.features.filter((f) => inR(f.properties.lat, f.properties.lon));
  const reports = raw.reports.filter((r) => inR(r.lat, r.lon) && state.types[r.cat]);
  const warnFeats = raw.warnings.features.filter((f) => {
    const c = featCenter(f); return inR(c[0], c[1]) && warnTypeOk(f.properties.ph);
  });
  const radarFeats = state.types.hail
    ? raw.radar_hail.features.filter((f) => { const c = featCenter(f); return inR(c[0], c[1]); })
    : [];
  const gridCells = raw.grid.cells.filter((c) => inR(c.lat, c.lon));
  const topN = raw.top_neighborhoods.filter((a) => inR(a.lat, a.lon)).slice(0, 25);
  const topAreas = raw.top_areas.filter((a) => inR(a.lat, a.lon));

  const stats = {
    neighborhoods: nbFeats.length,
    total: reports.length,
    warnings: warnFeats.length,
    max_hail_in: nbFeats.reduce((m, f) => Math.max(m, f.properties.max_hail_in || 0), 0),
    radar_max_hail_in: radarFeats.reduce((m, f) => Math.max(m, f.properties.hail_in || 0), 0),
  };

  latest = {
    ...raw, stats, reports,
    neighborhoods: { type: "FeatureCollection", features: nbFeats },
    warnings: { type: "FeatureCollection", features: warnFeats },
    radar_hail: { type: "FeatureCollection", features: radarFeats },
    grid: { cells: gridCells },
    top_neighborhoods: topN, top_areas: topAreas,
  };

  renderLegend(raw.grade_bands, raw.radar_legend);
  drawNeighborhoods(latest.neighborhoods);
  drawSwaths(latest.warnings);
  drawRadar(latest.radar_hail);
  drawHeat(latest.grid.cells);
  drawPins(latest.reports);
  applyLayerVisibility();
  renderTopList();

  const radarNote = state.layers.radar && stats.radar_max_hail_in ? ` · radar to ${stats.radar_max_hail_in}"` : "";
  document.getElementById("updated").textContent =
    `${raw.window_label} · ${stats.neighborhoods} ${state.unit === "bg" ? "blocks" : "ZIPs"} · ${stats.warnings} warnings${radarNote} · data ${fmtDate(raw.generated)}`;
}

// --------------------------------------------------------------------------- //
// Door-knocking address export — OpenStreetMap via Overpass, fully client-side
// --------------------------------------------------------------------------- //
function pointInRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
    if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
  }
  return inside;
}
function pointInPoly(x, y, poly) {
  if (!pointInRing(x, y, poly[0])) return false;
  for (let k = 1; k < poly.length; k++) if (pointInRing(x, y, poly[k])) return false;
  return true;
}
function pointInGeom(x, y, geom) {
  if (geom.type === "Polygon") return pointInPoly(x, y, geom.coordinates);
  if (geom.type === "MultiPolygon") return geom.coordinates.some((p) => pointInPoly(x, y, p));
  return false;
}
async function exportAddresses(id) {
  const lyr = nbLayers[id];
  if (!lyr) return;
  const geom = lyr.toGeoJSON().geometry;
  const b = lyr.getBounds();
  const bbox = `${b.getSouth()},${b.getWest()},${b.getNorth()},${b.getEast()}`;
  const q = `[out:json][timeout:60];(node["addr:housenumber"](${bbox});way["addr:housenumber"](${bbox}););out center tags;`;
  showBanner("Fetching street addresses from OpenStreetMap…");
  try {
    const res = await fetch("https://overpass-api.de/api/interpreter", {
      method: "POST", body: "data=" + encodeURIComponent(q),
    });
    if (!res.ok) throw new Error("overpass " + res.status);
    const json = await res.json();
    const rows = [];
    for (const el of json.elements) {
      const lat = el.lat != null ? el.lat : (el.center && el.center.lat);
      const lon = el.lon != null ? el.lon : (el.center && el.center.lon);
      if (lat == null || lon == null || !pointInGeom(lon, lat, geom)) continue;
      const t = el.tags || {};
      const street = [t["addr:housenumber"], t["addr:street"]].filter(Boolean).join(" ");
      rows.push([street, t["addr:city"] || "", t["addr:postcode"] || "", lat.toFixed(6), lon.toFixed(6)]);
      if (rows.length >= 6000) break;
    }
    rows.sort();
    const csv = ["address,city,postcode,lat,lon"].concat(rows.map((r) => r.map(csvCell).join(","))).join("\n");
    downloadBlob(`jem_addresses_${id}.csv`, csv, "text/csv");
    showBanner(rows.length ? "" : "No mapped street addresses found there (OpenStreetMap coverage varies).");
  } catch (e) {
    showBanner("Address lookup failed — OpenStreetMap/Overpass may be busy. Try again shortly.");
  }
}

// --------------------------------------------------------------------------- //
// Collapsible + drag-reorderable sidebar panels (persisted to localStorage)
// --------------------------------------------------------------------------- //

const PANELS_KEY = "jem.panels.v1";

function savePanelPrefs() {
  const container = document.getElementById("panels");
  const order = [...container.children].map((p) => p.dataset.panel);
  const collapsed = [...container.children]
    .filter((p) => p.classList.contains("collapsed"))
    .map((p) => p.dataset.panel);
  try { localStorage.setItem(PANELS_KEY, JSON.stringify({ order, collapsed })); } catch (e) { /* ignore */ }
}

function dragAfterElement(container, y) {
  const els = [...container.querySelectorAll(".panel:not(.dragging)")];
  return els.reduce((closest, child) => {
    const box = child.getBoundingClientRect();
    const offset = y - box.top - box.height / 2;
    if (offset < 0 && offset > closest.offset) return { offset, element: child };
    return closest;
  }, { offset: -Infinity, element: null }).element;
}

function initPanels() {
  const container = document.getElementById("panels");
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(PANELS_KEY)) || {}; } catch (e) { /* ignore */ }

  if (Array.isArray(prefs.order)) {
    prefs.order.forEach((id) => {
      const el = container.querySelector(`[data-panel="${id}"]`);
      if (el) container.appendChild(el);          // reorder to saved position
    });
  }
  if (Array.isArray(prefs.collapsed)) {
    prefs.collapsed.forEach((id) => {
      const el = container.querySelector(`[data-panel="${id}"]`);
      if (el) el.classList.add("collapsed");
    });
  }

  container.querySelectorAll(".panel").forEach((panel) => {
    const head = panel.querySelector(".panel-head");
    const handle = panel.querySelector(".drag-handle");

    head.addEventListener("click", (e) => {
      if (e.target.closest(".drag-handle")) return;  // grabbing handle, not collapsing
      if (panel.dataset.justDragged) { panel.dataset.justDragged = ""; return; }
      panel.classList.toggle("collapsed");
      savePanelPrefs();
    });

    // only allow dragging when the grip is grabbed (so inner controls stay usable)
    handle.addEventListener("mousedown", () => { panel.draggable = true; });
    handle.addEventListener("mouseup", () => { panel.draggable = false; });

    panel.addEventListener("dragstart", (e) => {
      panel.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", panel.dataset.panel);
    });
    panel.addEventListener("dragend", () => {
      panel.classList.remove("dragging");
      panel.draggable = false;
      panel.dataset.justDragged = "1";              // suppress the trailing click
      savePanelPrefs();
    });
  });

  container.addEventListener("dragover", (e) => {
    e.preventDefault();
    const dragging = container.querySelector(".panel.dragging");
    if (!dragging) return;
    const after = dragAfterElement(container, e.clientY);
    if (after == null) container.appendChild(dragging);
    else container.insertBefore(dragging, after);
  });
}

// --------------------------------------------------------------------------- //
// Hover tooltips for [data-tip] elements
// --------------------------------------------------------------------------- //
function initTooltips() {
  let tip = null;
  const hide = () => { if (tip) { tip.remove(); tip = null; } };
  const show = (el) => {
    hide();
    tip = document.createElement("div");
    tip.className = "tooltip";
    tip.textContent = el.getAttribute("data-tip");
    document.body.appendChild(tip);
    const r = el.getBoundingClientRect();
    const t = tip.getBoundingClientRect();
    let left = r.left;
    let top = r.bottom + 6;
    if (left + t.width > window.innerWidth - 8) left = window.innerWidth - t.width - 8;
    if (top + t.height > window.innerHeight - 8) top = r.top - t.height - 6;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, top) + "px";
    requestAnimationFrame(() => tip && tip.classList.add("show"));
  };
  document.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mouseenter", () => show(el));
    el.addEventListener("mouseleave", hide);
  });
}

initPanels();
initTooltips();
drawRadius();
load();
