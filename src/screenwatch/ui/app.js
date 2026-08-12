(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { spec: null, result: null, venues: [], toastTimer: null };

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[char]));

  async function api(path, options = {}) {
    const response = await fetch(path, { headers: { "content-type": "application/json", ...(options.headers || {}) }, ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`);
    return body;
  }

  function isoToday(offset = 0) {
    const date = new Date();
    date.setDate(date.getDate() + offset);
    const pad = (value) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function showToast(message, kind = "success") {
    const toast = $("#toast");
    toast.textContent = message;
    toast.className = `toast visible ${kind}`;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { toast.className = "toast"; }, 4500);
  }

  function buildPresentations() {
    const values = $$(".check-chip input:checked").map((input) => input.value);
    const specs = {
      imax70: { projection: "film_70mm_15perf", brand: "imax", label: "IMAX 70mm" },
      imax: { brand: "imax", label: "IMAX" },
      dolby: { brand: "dolby_cinema", label: "Dolby Cinema" },
      "70mm": { projection: "film_70mm", label: "70mm" },
      "35mm": { projection: "film_35mm", label: "35mm" },
      laser: { projection: "digital_laser", label: "Laser" },
    };
    return values.map((value) => specs[value]);
  }

  function buildLocation() {
    const location = {
      radius_km: Number($("#radius").value || 40),
      city: $("#city").value.trim() || null,
      allow: $$("#search-venues option:checked").map((option) => option.value),
      deny: [],
      chains: $("#chain-filter").value ? [$("#chain-filter").value] : [],
      venue_types: $("#type-filter").value ? [$("#type-filter").value] : [],
    };
    const lat = $("#lat").value;
    const lon = $("#lon").value;
    if (lat && lon) location.origin = { lat: Number(lat), lon: Number(lon) };
    return location;
  }

  function buildSpec({ strict = false } = {}) {
    const spec = {
      work: { query: $("#title").value.trim() },
      party_size: Math.max(1, Number($("#party").value || 1)),
      location: buildLocation(),
      date_window: { start: $("#from-date").value, end: $("#through-date").value },
      presentations: buildPresentations(),
      strict_presentations: strict,
      seating: {
        together: true,
        allow_split: true,
        party_kind: $("#party-kind").value,
        avoid_strangers: true,
      },
      include_sold_out: false,
      release_radar: $("#release-radar").checked,
      max_seatmap_fetches: 10,
      coverage: $("#coverage-mode").value || "auto",
    };
    return spec;
  }

  function formatTime(localIso, offset) {
    try {
      const match = String(localIso || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/);
      if (!match) return localIso;
      // `starts_at_local` is a venue wall-clock value, not a browser-local
      // instant. Render it on a UTC calendar so a Bay Area show does not move
      // to the viewer's timezone; the explicit offset keeps the source zone
      // visible without needing a browser timezone database lookup.
      const date = new Date(Date.UTC(
        Number(match[1]), Number(match[2]) - 1, Number(match[3]),
        Number(match[4]), Number(match[5]), Number(match[6] || 0),
      ));
      const label = new Intl.DateTimeFormat(undefined, {
        weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "UTC",
      }).format(date);
      return offset ? `${label} · UTC${offset}` : label;
    } catch { return localIso; }
  }

  function formatDistance(distance) {
    return distance == null ? "distance unknown" : `${Number(distance).toFixed(1)} km away`;
  }

  function renderOption(option, index) {
    const seats = option.seats;
    const estimate = option.seat_estimate;
    const seatTag = seats
      ? `${seats.complete ? "✓ party fits" : "partial fit"} · ${seats.together ? "together" : seats.cohesion}`
      : estimate
        ? `${Math.round(estimate.together_probability * 100)}% chance together`
        : option.seat_data === "unavailable" ? "seat data unavailable" : "seat check not run";
    const seatClass = seats?.complete || estimate?.can_fit ? "good" : option.seat_data === "unavailable" ? "warn" : "";
    const optimization = seats?.optimization;
    const proofTag = optimization?.proven_optimal
      ? `proven best · ${Number(optimization.combinations_considered || 0).toLocaleString()} layouts checked`
      : optimization
        ? `optimized · ${Number(optimization.candidates_evaluated || 0).toLocaleString()} finalists tested`
        : "";
    const alternativeTag = option.seat_alternatives?.length
      ? `+${option.seat_alternatives.length} meaningfully different layout${option.seat_alternatives.length === 1 ? "" : "s"}`
      : "";
    const alternatives = option.seat_alternatives?.length
      ? `<details class="seat-alternatives"><summary>Compare other strong seat layouts</summary><div>${option.seat_alternatives.map((group, alternativeIndex) => {
          const shape = (group.parts || []).map((part) => part.length).join("+") || `${group.count}`;
          const proof = group.optimization?.proven_optimal ? "proven optimal" : "optimized candidate";
          return `<div class="seat-alternative"><strong>${alternativeIndex + 1}. ${escapeHtml(group.labels)}</strong><span>${escapeHtml(shape)} · ${escapeHtml(group.cohesion.replaceAll("_", " "))} · worst person ${Math.round(Number(group.worst_person_utility || 0) * 100)}% · ${escapeHtml(proof)}</span></div>`;
        }).join("")}</div></details>`
      : "";
    const reasons = option.reasons?.slice(0, 2).join(" · ") || "Ranked across format, timing, venue, and availability.";
    const booking = option.booking_link ? `<a class="book-link" href="${escapeHtml(option.booking_link)}" target="_blank" rel="noreferrer">Book ↗</a>` : "";
    const seatmap = option.seat_data === "grid" && state.result?.search_id
      ? `<a class="seatmap-link" href="/v1/search/${encodeURIComponent(state.result.search_id)}/seatmap/${encodeURIComponent(option.option_id)}.svg" target="_blank" rel="noreferrer">view seat map</a>`
      : "";
    const sources = (option.source_listings || []).map((listing) => {
      const label = `${listing.source} · ${listing.availability}`;
      return listing.booking_link
        ? `<a class="tag source-link" href="${escapeHtml(listing.booking_link)}" target="_blank" rel="noreferrer">${escapeHtml(label)} ↗</a>`
        : `<span class="tag">${escapeHtml(label)}</span>`;
    }).join("");
    const runway = state.result?.search_id
      ? `<button class="button button-quiet runway-button" data-runway-option="${escapeHtml(option.option_id)}" type="button">Plan checkout</button>` : "";
    return `<article class="result-card">
      <div class="result-rank">${index + 1}</div>
      <div class="result-main">
        <div class="result-title-row"><span class="result-time">${escapeHtml(formatTime(option.starts_at_local, option.starts_at_local_offset))}</span><span class="result-format">${escapeHtml(option.presentation)}</span></div>
        <div class="result-venue">${escapeHtml(option.venue.name)} <span class="tag">${escapeHtml(option.venue.chain)}</span></div>
        <div class="result-subline"><span class="tag ${seatClass}">${escapeHtml(seatTag)}</span>${proofTag ? `<span class="tag" title="${escapeHtml(optimization.scope || "")}">${escapeHtml(proofTag)}</span>` : ""}${alternativeTag ? `<span class="tag">${escapeHtml(alternativeTag)}</span>` : ""}<span class="tag">${escapeHtml(formatDistance(option.venue.distance_km))}</span><span class="tag">${escapeHtml(option.availability)}</span>${sources}${seatmap ? `<span class="tag">${seatmap}</span>` : ""}</div>
        <div class="result-reasons"><strong>Why this is here:</strong> ${escapeHtml(reasons)}</div>
        ${alternatives}
      </div>
      <div class="result-side"><div><div class="score">${Math.round(option.score * 100)}<small>%</small></div><div class="score-label">fit score</div></div>${runway}${booking}</div>
    </article>`;
  }

  function bindRunwayButtons(root = document) {
    $$('[data-runway-option]', root).forEach((button) => button.addEventListener("click", () => loadBookingRunway(button.dataset.runwayOption)));
  }

  async function loadBookingRunway(optionId) {
    if (!state.result?.search_id) return;
    const target = $("#booking-runway");
    target.hidden = false;
    target.innerHTML = `<div class="loading-state"><span class="spinner"></span><span>Dividing seats across signed-in profiles…</span></div>`;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    try {
      const payload = await api(`/v1/search/${encodeURIComponent(state.result.search_id)}/booking-runway/${encodeURIComponent(optionId)}`, {
        method: "POST",
        body: JSON.stringify({
          party_size: state.spec.party_size,
          transaction_limit: Math.max(1, Number($("#checkout-limit").value || 10)),
          parallel_checkouts: Math.max(1, Number($("#checkout-lanes").value || 2)),
        }),
      });
      const lanes = payload.lanes.map((lane) => `<article class="checkout-lane"><div class="lane-number">${lane.lane}</div><div class="lane-main"><span class="status-kicker">WAVE ${lane.wave} · ${escapeHtml(lane.profile)}</span><h4>${lane.ticket_count} tickets</h4><p>${lane.seat_label ? `Select ${escapeHtml(lane.seat_label)}` : "Select this block from the staged seat recommendation."}</p></div>${lane.booking_link ? `<a class="button button-primary" href="${escapeHtml(lane.booking_link)}" target="_blank" rel="noreferrer">Open lane ${lane.lane} ↗</a>` : `<span class="tag warn">link pending</span>`}</article>`).join("");
      target.innerHTML = `<div class="runway-heading"><div><span class="status-kicker">BOOKING RUNWAY · ${escapeHtml(payload.readiness)}</span><h3>${escapeHtml(payload.option.venue)} · ${escapeHtml(payload.split.join(" + "))}</h3><p>${escapeHtml(payload.handoff)}</p></div><span class="tag ${payload.readiness === "ready" ? "good" : "warn"}">${payload.exact_seat_assignment ? "exact seats assigned" : "seat assignment pending"}</span></div>${payload.warning ? `<div class="runway-warning">${escapeHtml(payload.warning)}</div>` : ""}<div class="checkout-lanes">${lanes}</div><div class="runway-protocol"><strong>Commit protocol</strong><span>Both profiles signed in → both carts match the seam → one person calls “submit” → both buyers purchase. If one cart fails, do not silently move the other block.</span></div>`;
    } catch (error) {
      target.innerHTML = `<div class="runway-warning">${escapeHtml(error.message)}</div>`;
    }
  }

  function renderResults(result) {
    state.result = result;
    const empty = $("#results-empty");
    const loading = $("#results-loading");
    const list = $("#results-list");
    loading.hidden = true;
    empty.hidden = result.options.length > 0;
    list.innerHTML = result.options.map(renderOption).join("");
    bindRunwayButtons(list);
    $("#result-meta").textContent = `${result.options.length} ranked options · ${result.considered} considered · ${result.coverage || "nearby"} coverage · ${Math.round(result.duration_ms)}ms`;
    const coverageNote = result.coverage === "exhaustive"
      ? "Exhaustive source traversal requested"
      : "Fast bounded source traversal requested";
    const notes = [coverageNote, ...(result.provider_errors || []), ...(result.clipped || [])];
    const scopeNote = $("#scope-note");
    scopeNote.hidden = notes.length === 0;
    scopeNote.innerHTML = `<strong>Coverage note:</strong> ${notes.map(escapeHtml).join(" · ")}`;
  }

  async function runSearch(event) {
    event?.preventDefault();
    if (!$("#from-date").value) $("#from-date").value = isoToday();
    if (!$("#through-date").value) $("#through-date").value = isoToday(7);
    const loading = $("#results-loading");
    $("#results-empty").hidden = true;
    $("#results-list").innerHTML = "";
    loading.hidden = false;
    $("#result-meta").textContent = "Reading the theater graph…";
    try {
      state.spec = buildSpec();
      const result = await api("/v1/search", { method: "POST", body: JSON.stringify(state.spec) });
      renderResults(result);
      showToast(result.complete ? "Search complete — options are ranked by what you can actually book." : "Search complete with coverage notes; read the scope warning below the results.");
    } catch (error) {
      loading.hidden = true;
      $("#results-empty").hidden = false;
      $("#result-meta").textContent = "Search failed";
      showToast(error.message, "error");
    }
  }

  function renderWatches(watches) {
    const target = $("#watches-list");
    if (!watches.length) { target.innerHTML = `<div class="mini-empty">No active watches yet.</div>`; return; }
    target.innerHTML = watches.map((watch) => {
      const status = watch.last_error ? "needs attention" : watch.last_success ? "healthy" : "new";
      const detail = watch.last_error || (watch.last_hit ? `last hit ${new Date(watch.last_hit).toLocaleString()}` : `polls every ${Math.round(watch.cadence_s / 60)} min`);
      return `<div class="watch-row"><div class="watch-row-top"><span class="watch-label">${escapeHtml(watch.label)}</span><span class="watch-state">${escapeHtml(status)}</span></div><div class="watch-row-meta">${escapeHtml(detail)} <button class="watch-cancel" data-cancel-watch="${escapeHtml(watch.watch_id)}">cancel</button></div></div>`;
    }).join("");
    $$('[data-cancel-watch]').forEach((button) => button.addEventListener("click", async () => {
      try { await api(`/v1/watches/${encodeURIComponent(button.dataset.cancelWatch)}`, { method: "DELETE" }); await loadWatches(); showToast("Watch cancelled."); }
      catch (error) { showToast(error.message, "error"); }
    }));
  }

  function renderAlertHistory(entries) {
    const target = $("#alert-history");
    if (!entries.length) {
      target.innerHTML = `<div class="mini-empty">Alerts you acknowledge will remain visible here.</div>`;
      return;
    }
    target.innerHTML = entries.map((entry) => {
      const payload = entry.payload || {};
      const detail = [payload.title, payload.venue, payload.presentation].filter(Boolean).join(" · ");
      const when = entry.created_at ? new Date(entry.created_at).toLocaleString() : "recently";
      return `<div class="alert-row"><div class="alert-row-top"><span class="alert-kind">${escapeHtml(payload.alert_type || "alert")}</span><span class="alert-time">${escapeHtml(when)}</span></div><div class="alert-detail">${escapeHtml(detail || "Screenwatch observed a change")}</div><div class="alert-watch">${escapeHtml(entry.watch_label || "watch")}${entry.delivered ? " · acknowledged" : " · pending"}</div></div>`;
    }).join("");
  }

  async function loadAlertHistory(watches) {
    if (!watches.length) { renderAlertHistory([]); return; }
    try {
      const histories = await Promise.all(watches.slice(0, 10).map(async (watch) => {
        const history = await api(`/v1/watches/${encodeURIComponent(watch.watch_id)}/history?limit=8`);
        return (history.hits || []).map((hit) => ({ ...hit, watch_label: watch.label }));
      }));
      const entries = histories.flat().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).slice(0, 8);
      renderAlertHistory(entries);
    } catch (error) { showToast(error.message, "error"); }
  }

  async function loadWatches() {
    try {
      const watches = await api("/v1/watches");
      renderWatches(watches);
      await loadAlertHistory(watches);
    } catch (error) { showToast(error.message, "error"); }
  }

  async function createWatch(event) {
    event.preventDefault();
    if (!$("#title").value.trim()) {
      showToast("Enter a film or event before creating a watch.", "error");
      $("#title").focus();
      return;
    }
    state.spec = buildSpec({ strict: true });
    if ($("#watch-rolling").checked) state.spec.date_window = null;
    const label = $("#watch-label").value.trim() || `${state.spec.work.query} watch`;
    const spec = { ...state.spec, strict_presentations: state.spec.presentations?.length > 0, include_sold_out: true };
    try {
      await api("/v1/watches", { method: "POST", body: JSON.stringify({ label, spec, seed: $("#watch-seed").checked, cadence_s: Number($("#watch-cadence").value || 300) }) });
      $("#watch-label").value = "";
      await loadWatches();
      showToast("Watch created. It will stay quiet about tickets already on sale.");
    } catch (error) { showToast(error.message, "error"); }
  }

  function announceHits(hits) {
    if (!hits.length) return;
    const first = hits[0];
    const message = `${first.title || "New screening"} · ${first.venue || "theater"} · ${first.presentation || "tickets"}`;
    showToast(`${hits.length} alert${hits.length === 1 ? "" : "s"}: ${message}`);
    if ("Notification" in window && Notification.permission === "granted") new Notification("screenwatch alert", { body: message });
  }

  async function pollWatches() {
    try {
      const payload = await api("/v1/watches/poll", { method: "POST" });
      const hits = payload.hits || [];
      announceHits(hits);
      const hitIds = hits.map((hit) => hit.hit_id).filter((hitId) => Number.isInteger(hitId));
      if (hitIds.length) {
        // Keep the durable queue pending until the toast/notification has
        // actually been emitted. A failed acknowledgement leaves the alert
        // available for the next poll instead of losing it before display.
        await api("/v1/watches/acknowledge", {
          method: "POST",
          body: JSON.stringify({ hit_ids: hitIds }),
        });
      }
      await loadWatches();
      await loadOverview();
      await loadAnalytics();
    } catch (error) { showToast(error.message, "error"); }
  }

  function renderOverview(meta) {
    const inventory = meta.inventory || {};
    $("#metric-screenings").textContent = Number(inventory.screenings || 0).toLocaleString();
    $("#metric-venues").textContent = Number(meta.directory?.venues || 0).toLocaleString();
    const exact = (meta.providers || []).filter((p) => p.seat_data === "exact").length;
    $("#metric-seat-data").textContent = exact ? `${exact} exact` : "warming";
    $("#metric-alerts").textContent = Number(inventory.alerts?.pending || 0).toLocaleString();
    const health = meta.provider_health || [];
    $("#provider-health").innerHTML = health.length
      ? health.map((provider) => `<span class="health-pill ${escapeHtml(provider.health)}"><span></span>${escapeHtml(provider.chain)} · ${escapeHtml(provider.health)}${provider.last_status === "error" ? " · needs attention" : ""}</span>`).join("")
      : `<span class="health-empty">Source health appears after the first search.</span>`;
    const evidence = meta.evidence || {};
    const kinds = (evidence.by_kind || []).map((item) => `${Number(item.observations || 0).toLocaleString()} ${item.kind.replaceAll("_", " ")}`).join(" · ");
    $("#data-trust-note").textContent =
      `Data trust: ${Number(evidence.observations || 0).toLocaleString()} source observations${kinds ? ` · ${kinds}` : ""}. No permanent room hardware is inferred from a listing.`;
  }

  async function loadOverview() {
    try { renderOverview(await api("/v1/analytics/overview")); } catch (error) { showToast(error.message, "error"); }
  }

  function renderAnalytics(payload) {
    const target = $("#inventory-analytics");
    const groups = payload.groups || [];
    if (!groups.length) {
      target.innerHTML = `<div class="table-empty">No indexed evidence yet. Run a search or refresh sources.</div>`;
      return;
    }
    const percent = (value) => value == null ? "—" : `${Math.round(Number(value) * 100)}%`;
    target.innerHTML = `<div class="table-wrap"><table class="analytics-table"><thead><tr><th>Dimension</th><th>Screenings</th><th>Venues</th><th>Sellable</th><th>Seat coverage</th><th>Open / capacity</th></tr></thead><tbody>${groups.map((group) => {
      const seats = Number(group.seats_capacity || 0) > 0
        ? `${Number(group.seats_available || 0).toLocaleString()} / ${Number(group.seats_capacity || 0).toLocaleString()}`
        : "—";
      return `<tr><td class="analytics-key">${escapeHtml(group.group_label || group.group_key)}</td><td>${Number(group.screenings || 0).toLocaleString()}<span class="venue-chain">${Number(group.works || 0).toLocaleString()} works</span></td><td>${Number(group.venues || 0).toLocaleString()}</td><td>${Number(group.sellable || 0).toLocaleString()}</td><td>${percent(group.seat_coverage)}</td><td>${escapeHtml(seats)}<span class="venue-chain">${group.seat_fill == null ? "no fill estimate" : `${percent(group.seat_fill)} occupied`}</span></td></tr>`;
    }).join("")}</tbody></table></div><div class="analytics-caveat">${escapeHtml(payload.caveat || "")}</div>`;
  }

  async function loadAnalytics() {
    try {
      const group = $("#analytics-group").value || "chain";
      renderAnalytics(await api(`/v1/analytics/inventory?group_by=${encodeURIComponent(group)}`));
    } catch (error) { showToast(error.message, "error"); }
  }

  function renderVenues(rows) {
    state.venues = rows;
    const counts = rows.reduce((acc, venue) => { acc[venue.type_label] = (acc[venue.type_label] || 0) + 1; return acc; }, {});
    $("#venue-summary").innerHTML = Object.entries(counts).slice(0, 6).map(([name, count]) => `<span class="summary-pill"><strong>${count}</strong> ${escapeHtml(name)}</span>`).join("");
    $("#venues-table").innerHTML = rows.map((venue) => {
      const inventory = venue.inventory || {};
      const capabilities = venue.capabilities?.slice(0, 2).map((cap) => cap.label).join(" · ") || "No observed presentation yet";
      const evidenceStatus = venue.evidence?.observations
        ? `${Number(venue.evidence.observations).toLocaleString()} source observations`
        : "No source observations yet";
      const seatRollup = Number(inventory.seat_screenings || 0) > 0
        ? `${Number(inventory.seats_available || 0).toLocaleString()} / ${Number(inventory.seats_capacity || 0).toLocaleString()} seats open`
        : "No live seat count yet";
      return `<tr><td><span class="venue-name">${escapeHtml(venue.name)}</span><span class="venue-chain">${escapeHtml(venue.chain)}${venue.distance_km != null ? ` · ${Number(venue.distance_km).toFixed(1)} km` : ""}</span></td><td><span class="venue-type">${escapeHtml(venue.type_label)}</span></td><td><span class="seat-surface">${escapeHtml(venue.seat_surface)}</span><span class="venue-chain">${escapeHtml(venue.seat_detail)}</span></td><td>${Number(inventory.screenings || 0).toLocaleString()} screenings<span class="venue-chain">${Number(inventory.works || 0).toLocaleString()} works · ${Number(inventory.sellable || 0).toLocaleString()} sellable</span><span class="venue-chain">${escapeHtml(seatRollup)}</span></td><td>${escapeHtml(capabilities)}<span class="venue-chain">${escapeHtml(evidenceStatus)}</span></td></tr>`;
    }).join("") || `<tr><td colspan="5" class="table-empty">No venues match that filter.</td></tr>`;
  }

  async function loadVenues() {
    try {
      const params = new URLSearchParams({
        sort: $("#venue-sort").value || "distance",
        q: $("#venue-filter").value || "",
        chain: $("#venue-chain").value || "",
        type: $("#venue-type").value || "",
        city: $("#city").value || "",
        radius_km: $("#radius").value || "",
        include_unknown: "true",
        lat: $("#lat").value || "",
        lon: $("#lon").value || "",
      });
      if (!$("#lat").value || !$("#lon").value) {
        params.delete("lat");
        params.delete("lon");
      }
      const payload = await api(`/v1/venues?${params}`);
      renderVenues(payload.venues || []);
    } catch (error) { showToast(error.message, "error"); }
  }

  async function loadVenueChoices() {
    try {
      const payload = await api("/v1/venues?sort=name&limit=1000");
      $("#search-venues").innerHTML = (payload.venues || []).map((venue) =>
        `<option value="${escapeHtml(venue.id)}">${escapeHtml(venue.name)} · ${escapeHtml(venue.chain)}</option>`
      ).join("");
    } catch (error) { showToast(error.message, "error"); }
  }

  async function refreshVenues() {
    const button = $("#refresh-venues");
    button.disabled = true;
    button.textContent = "Refreshing…";
    try {
      const result = await api("/v1/venues/refresh", {
        method: "POST",
        body: JSON.stringify(buildLocation()),
      });
      await Promise.all([loadVenues(), loadOverview(), loadAnalytics()]);
      const note = result.errors?.length ? ` with ${result.errors.length} source note${result.errors.length === 1 ? "" : "s"}` : "";
      const coverage = result.complete ? "complete" : "degraded — inspect source notes";
      showToast(`National venue directory refreshed — ${Number(result.discovered || 0).toLocaleString()} records observed · ${coverage}${note}.`);
    } catch (error) { showToast(error.message, "error"); }
    finally { button.disabled = false; button.textContent = "Refresh sources"; }
  }

  async function loadHealth() {
    const status = $("#health-status");
    try { await api("/v1/health"); status.className = "status-dot ready"; status.innerHTML = "<span></span>local engine ready"; }
    catch { status.className = "status-dot error"; status.innerHTML = "<span></span>engine unavailable"; }
  }

  function bind() {
    $("#search-form").addEventListener("submit", runSearch);
    $("#watch-form").addEventListener("submit", createWatch);
    $("#poll-button").addEventListener("click", pollWatches);
    $("#venue-filter").addEventListener("input", () => { clearTimeout(state.venueTimer); state.venueTimer = setTimeout(loadVenues, 180); });
    $("#refresh-venues").addEventListener("click", refreshVenues);
    $("#venue-chain").addEventListener("change", loadVenues);
    $("#venue-type").addEventListener("change", loadVenues);
    $("#venue-sort").addEventListener("change", loadVenues);
    $("#analytics-group").addEventListener("change", loadAnalytics);
    $("#locate-button").addEventListener("click", () => {
      if (!("geolocation" in navigator)) { showToast("This browser does not expose geolocation.", "error"); return; }
      const button = $("#locate-button");
      button.disabled = true;
      button.textContent = "Locating…";
      navigator.geolocation.getCurrentPosition(
        (position) => {
          $("#lat").value = position.coords.latitude.toFixed(5);
          $("#lon").value = position.coords.longitude.toFixed(5);
          button.disabled = false;
          button.textContent = "Location set";
          showToast("Location set — searches will rank nearby venues first.");
          loadVenues();
        },
        (error) => {
          button.disabled = false;
          button.textContent = "Use my location";
          showToast(error.code === 1 ? "Location permission was denied." : "Could not determine your location.", "error");
        },
        { enableHighAccuracy: false, maximumAge: 300000, timeout: 10000 },
      );
    });
    $("#notify-button").addEventListener("click", async () => {
      if (!("Notification" in window)) { showToast("Browser notifications are not available here.", "error"); return; }
      const permission = await Notification.requestPermission();
      showToast(permission === "granted" ? "Browser alerts enabled." : "Browser alerts remain disabled.", permission === "granted" ? "success" : "error");
    });
    window.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#title").focus(); } });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    $("#from-date").value = isoToday();
    $("#through-date").value = isoToday(7);
    bind();
    await Promise.all([loadHealth(), loadOverview(), loadAnalytics(), loadWatches(), loadVenues(), loadVenueChoices()]);
    setInterval(pollWatches, 60_000);
  });
})();
