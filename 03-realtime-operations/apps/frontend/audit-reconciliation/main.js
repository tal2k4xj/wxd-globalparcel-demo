const map = L.map("routeMap", { zoomControl: true });
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap"
}).addTo(map);
const routeLayer = L.layerGroup().addTo(map);
map.setView([20, 0], 2);
requestAnimationFrame(() => map.invalidateSize());

function formatGeo(geo) {
  if (!geo || geo.latitude == null || geo.longitude == null) {
    return "-";
  }
  return `${geo.latitude.toFixed(4)}, ${geo.longitude.toFixed(4)}`;
}

function renderMap(rows) {
  routeLayer.clearLayers();
  const points = rows
    .map((row) => ({
      lat: row.geo_position?.latitude,
      lon: row.geo_position?.longitude,
      status: row.status,
      event_ts: row.event_ts,
      hub_code: row.hub_code
    }))
    .filter((p) => p.lat != null && p.lon != null);

  if (!points.length) {
    map.setView([20, 0], 2);
    map.invalidateSize();
    return;
  }

  const chronological = [...points].reverse();
  const latLngs = chronological.map((p) => [p.lat, p.lon]);
  L.polyline(latLngs, { color: "#2563eb", weight: 4, opacity: 0.9 }).addTo(routeLayer);

  chronological.forEach((p, idx) => {
    const isLatest = idx === chronological.length - 1;
    L.circleMarker([p.lat, p.lon], {
      radius: isLatest ? 7 : 5,
      color: isLatest ? "#4ade80" : "#93c5fd",
      fillColor: isLatest ? "#4ade80" : "#60a5fa",
      fillOpacity: 0.9,
      weight: 1
    })
      .bindPopup(`${p.status}<br>${p.hub_code}<br>${p.event_ts}`)
      .addTo(routeLayer);
  });

  map.fitBounds(latLngs, { padding: [24, 24], maxZoom: 6 });
  map.invalidateSize();
}

function renderTimeline(parcelId, rows) {
  const tbody = document.getElementById("timelineRows");
  tbody.innerHTML = "";

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="7">No events found for ${parcelId}. Verify Cassandra partition key.</td></tr>`;
    return;
  }

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const cols = [
      row.event_ts || "-",
      row.status || "-",
      row.hub_code || "-",
      row.region || "-",
      formatGeo(row.geo_position),
      row.exception_code || "-",
      row.delivery_note || "-"
    ];
    cols.forEach((col) => {
      const td = document.createElement("td");
      td.textContent = col;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

async function loadAuditData() {
  const parcelId = document.getElementById("parcelId").value.trim();
  document.getElementById("summaryParcel").textContent = parcelId;
  const res = await fetch(`/api/audit/${encodeURIComponent(parcelId)}`);
  if (!res.ok) {
    renderTimeline(parcelId, []);
    renderMap([]);
    document.getElementById("truthStatus").textContent = "NOT_FOUND";
    document.getElementById("appStatus").textContent = "UNKNOWN";
    document.getElementById("reconResult").textContent = "Unable to reconcile";
    document.getElementById("reconResult").className = "warn";
    return;
  }
  const data = await res.json();
  const timeline = data.timeline || [];
  renderTimeline(parcelId, timeline);
  renderMap(timeline);

  const summary = data.summary || {};
  document.getElementById("appStatus").textContent = summary.customer_app_status || "UNKNOWN";
  document.getElementById("truthStatus").textContent = summary.cassandra_latest_status || "UNKNOWN";
  document.getElementById("reconResult").textContent = summary.reconciliation_result || "Unknown";
  document.getElementById("reconResult").className = summary.index_lag_suspected ? "warn" : "ok";
  renderSignals(summary);
}

function setSignal(id, text, cls, detail) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = cls;
  document.getElementById(id.replace("Signal", "Detail")).textContent = detail;
}

function renderSignals(summary) {
  const notes = document.getElementById("auditNotes");
  if (summary.customer_app_status === "UNAVAILABLE") {
    setSignal("lagSignal", "Unknown", "warn", "OpenSearch did not answer");
    setSignal("actionSignal", "Check index", "warn", "Customer view cannot be compared");
    notes.value = "Customer index unreachable; reconcile once OpenSearch is back.";
  } else if (summary.index_lag_suspected) {
    setSignal("lagSignal", "Likely", "warn",
      `Customer sees ${summary.customer_app_status} (${summary.customer_app_event_ts || "not indexed"}); ledger has ${summary.cassandra_latest_status} (${summary.cassandra_latest_event_ts})`);
    setSignal("actionSignal", "Recommended", "warn", "Trigger customer notification refresh");
    notes.value = `Source of truth shows ${summary.cassandra_latest_status}; customer-facing view still shows ${summary.customer_app_status} (index lag). Recommend push refresh and close ticket after acknowledgment.`;
  } else {
    setSignal("lagSignal", "None", "ok", `Customer view and ledger agree: ${summary.cassandra_latest_status}`);
    setSignal("actionSignal", "None needed", "ok", "Use the ledger timeline as evidence");
    notes.value = `Customer view and Cassandra ledger agree (${summary.cassandra_latest_status}). Close the dispute with the ledger timeline and driver note as evidence.`;
  }
}

function cell(tr, text) {
  const td = document.createElement("td");
  td.textContent = text;
  tr.appendChild(td);
  return td;
}

async function runSearch(q) {
  const stats = document.getElementById("searchStats");
  const facets = document.getElementById("searchFacets");
  const table = document.getElementById("searchTable");
  const tbody = document.getElementById("searchRows");
  document.getElementById("searchQuery").value = q;
  if (!q.trim()) return;
  stats.textContent = "Searching…";
  const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
  if (!res.ok) {
    stats.textContent = `Search failed (${res.status})`;
    table.hidden = true;
    facets.innerHTML = "";
    return;
  }
  const data = await res.json();
  stats.textContent = `${data.matching_scans.toLocaleString()} matching scans across ${data.matching_parcels.toLocaleString()} parcels in ${data.took_ms} ms — showing the best match per parcel. Click a row to audit it.`;
  facets.innerHTML = "";
  [["Hub", data.by_hub], ["Status", data.by_status]].forEach(([label, buckets]) => {
    if (!buckets.length) return;
    const row = document.createElement("div");
    row.className = "facet-row";
    const name = document.createElement("span");
    name.textContent = `${label}:`;
    row.appendChild(name);
    buckets.forEach((b) => {
      const tag = document.createElement("span");
      tag.className = "facet";
      tag.textContent = `${b.key} ${b.count.toLocaleString()}`;
      row.appendChild(tag);
    });
    facets.appendChild(row);
  });
  tbody.innerHTML = "";
  data.results.forEach((r) => {
    const tr = document.createElement("tr");
    tr.className = "clickable";
    cell(tr, r.parcel_id);
    cell(tr, r.event_ts || "-");
    cell(tr, r.status || "-").className = r.sla_risk ? "warn" : "";
    cell(tr, `${r.hub_code || "-"} ${r.hub_city || ""}`);
    // Highlight is HTML-encoded by OpenSearch (encoder: html); only <mark> tags are markup.
    const note = document.createElement("td");
    if (r.highlight) note.innerHTML = r.highlight;
    else note.textContent = r.delivery_note || "-";
    tr.appendChild(note);
    tr.addEventListener("click", () => {
      document.getElementById("parcelId").value = r.parcel_id;
      loadAuditData();
      document.querySelector(".search-card").scrollIntoView({ behavior: "smooth" });
    });
    tbody.appendChild(tr);
  });
  table.hidden = !data.results.length;
}

document.getElementById("searchForm").addEventListener("submit", (e) => {
  e.preventDefault();
  runSearch(document.getElementById("searchQuery").value);
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => runSearch(chip.dataset.q));
});

document.getElementById("loadBtn").addEventListener("click", () => {
  loadAuditData();
});

document.getElementById("resolveBtn").addEventListener("click", () => {
  document.getElementById("reconResult").textContent = "Source-of-truth confirmed - ticket ready to close";
  document.getElementById("reconResult").className = "ok";
});

loadAuditData();
