"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let status = null, busy = false, expired = false, polling = null, retrying = false;
  let siteOffset = 0, siteTotal = 0, sessionOffset = 0, sessionTotal = 0, sites = [], sessionsLoading = false;
  let previewReading = null, section = "collection", registrationGeneration = 0;
  const pageSize = 20;
  const show = (id, text) => { $(id).textContent = text; };
  const value = v => v === null || v === undefined ? "Unavailable" : String(v);
  function clearRegistration() {
    registrationGeneration++;
    $("registration-text").value = "";
    $("registration-display").replaceChildren();
    $("registration-label").hidden = $("copy").hidden = true;
    show("copy-help", "");
  }
  function failure(error) {
    $("error").hidden = false;
    const code = error.code || "connection_failed";
    show("error", `${code}: ${error.message || "Request failed."} Reload status before retrying; a timeout does not prove rollback.`);
    if (code === "unauthorized") {
      expired = true; clearRegistration(); $("login-link").hidden = false;
      show("error", "Login expired. Log in again to continue.");
    }
    if (code === "pending_uploads_prevent_server_change") {
      show("error", "Server origin cannot change while uploads are pending. Restore access to the saved server, register this Logger there, then Retry upload until the backlog clears. Your entered settings are still available.");
    }
    controls();
  }
  async function api(path, method = "GET", body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 22000);
    try {
      const response = await fetch(`/api/local/v1${path}`, {
        method, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: method === "GET" ? {} : {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        ...(body === undefined ? {} : {body: JSON.stringify(body)})
      });
      const result = await response.json();
      if (!response.ok) throw result.error;
      return result.data;
    } finally { clearTimeout(timeout); }
  }
  function controls() {
    const transitioning = status && ["starting", "stopping", "previewing"].includes(status.logger.collection_state);
    const serialFault = status && ["serial_close_failed", "serial_join_failed"].includes(status.runtime_error);
    document.querySelectorAll("[data-mutation]").forEach(b => b.disabled = busy || expired || !status || transitioning || serialFault);
    $("retry-upload").disabled = expired || !status || retrying;
    const recovery = $("serial-recovery");
    recovery.hidden = !serialFault;
    recovery.textContent = serialFault
      ? `Serial resource failure (${status.runtime_error}). Serial operations are unavailable because the device may still be owned. Restart the Logger container to recover; this preserves its data volume.`
      : "";
    if (!status) return;
    const open = status.active_session_state === "open";
    $("start").disabled ||= !open || status.persisted_blocks.length > 0;
    $("pause").disabled ||= !open;
    $("complete").disabled ||= !open;
    $("meter-confirm").hidden = !status.persisted_blocks.includes("meter_changed");
    $("storage-resolve").hidden = !status.logger.storage_blocked;
  }
  function details(id, entries) {
    $(id).replaceChildren();
    for (const [label, content] of entries) {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = label; dd.textContent = value(content); $(id).append(dt, dd);
    }
  }
  function renderStatus(next) {
    if (status && status.logger.session_id !== next.logger.session_id) previewReading = null;
    status = next;
    const s = next.logger;
    $("synthetic").hidden = next.input_mode !== "synthetic";
    details("status", [["Collection", s.collection_state], ["Session state", next.active_session_state],
      ["Device UUID", s.device_id], ["Site UUID", s.site_id], ["Session UUID", s.session_id],
      ["Source UUID", s.source_id], ["Measurement point UUID", s.measurement_point_id],
      ["Latest saved current-session reception (UTC)", next.active_session_latest_received_at],
      ["Latest saved reception across all sessions (UTC)", s.latest_received_at],
      ["All-session pending records", s.pending_count], ["All-session pending bytes", s.pending_bytes],
      ["Oldest all-session pending reception (UTC)", s.oldest_pending_received_at], ["Free storage bytes", s.free_bytes],
      ["Persisted blocks", next.persisted_blocks.join(", ") || "None"],
      ["Collection error", s.latest_collection_error || "None"], ["Runtime error", next.runtime_error || "None"],
      ["Last successful upload checkpoint (UTC)", s.last_successful_upload_at], ["Upload mode", next.upload_mode]]);
    const up = next.upload;
    details("upload-status", [["Mode", next.upload_mode], ["State", up.state],
      ["Next attempt (UTC)", up.next_attempt_at], ["Global upload error", up.global_error || "None"],
      ["Blocked session groups", up.blocked_group_count], ["All-session pending count", s.pending_count],
      ["All-session pending bytes", s.pending_bytes], ["Oldest pending reception (UTC)", s.oldest_pending_received_at],
      ["Last durable confirmation checkpoint (UTC)", s.last_successful_upload_at]]);
    const errorText = up.global_error ? ` Upload is ${up.state}: ${explainError(up.global_error)}.` : "";
    show("upload-help", next.upload_mode === "unconfigured"
      ? "No Server origin is saved, so the Logger makes no upload request. Save an origin to enable automatic recovery."
      : `Uploads run automatically. Retry schedules another attempt and does not confirm delivery.${errorText}`);
    show("upload-banner", next.upload_mode === "unconfigured" ? "Upload is unconfigured; saved snapshots remain pending until a Server origin is saved."
      : `Upload ${up.state}; ${s.pending_count} snapshots remain pending.${errorText}`);
    $("retry-upload").disabled = expired || !status || retrying;
    if (section === "maintenance" && !sessionsLoading) loadSessions().catch(failure);
    renderReading(previewReading || next.latest_live_reading, previewReading ? "Read preview (not stored)" : "Live reading (not proof of storage)");
    controls();
  }
  function explainError(code) {
    const text = {
      invalid_credential: "the Server rejected the registration Token; reveal and register the current Logger identity, then Retry",
      site_not_authorized: "the Server denied this identity for the session site; authorize that site, then Retry",
      device_mismatch: "the Server identity does not match this Logger; check registration before Retry",
      upload_network_error: "the Server could not be reached; check connectivity",
      upload_timeout: "the request timed out; records remain pending and will be retried",
      upload_endpoint_error: "the saved Server endpoint did not accept ingestion; verify the saved origin",
      upload_worker_failed: "the local upload worker stopped and needs a Logger restart",
      upload_storage_error: "the confirmation could not be saved locally; check storage",
      upload_protocol_error: "the Server response could not be confirmed; records remain pending",
      invalid_confirmation: "the Server response could not be verified; records remain pending",
      upload_http_rejected: "the Server rejected this session group; check its registration and data"
    };
    return text[code] || "the request failed; records remain pending";
  }
  function renderReading(reading, kind) {
    show("reading-kind", reading ? kind : "No live reading available.");
    details("reading-times", [["Original meter time", reading?.meter_time_raw], ["Meter time (UTC)", reading?.meter_time_utc], ["Reception (UTC)", reading?.received_at]]);
    $("metrics").replaceChildren();
    const names = ["energy_import_t1_kwh", "energy_import_t2_kwh", "energy_export_t1_kwh", "energy_export_t2_kwh", "power_import_kw", "power_export_kw"];
    for (const group of ["voltage", "current", "power_import", "power_export"]) for (let phase = 1; phase <= 3; phase++) names.push(`${group}_l${phase}_${group === "voltage" ? "v" : group === "current" ? "a" : "kw"}`);
    for (const name of names) {
      const row = document.createElement("tr");
      for (const text of [name.replaceAll("_", " "), value(reading?.metrics[name]), {kwh: "kWh", kw: "kW", v: "V", a: "A"}[name.split("_").at(-1)]]) {
        const cell = document.createElement("td"); cell.textContent = text; row.append(cell);
      }
      $("metrics").append(row);
    }
    show("fields", reading ? JSON.stringify(reading.fields, null, 2) : "Unavailable");
    show("quality", reading ? JSON.stringify(reading.quality, null, 2) : "Unavailable");
  }
  async function refreshStatus() {
    if (expired) return;
    if (polling) return polling;
    polling = api("/status").then(renderStatus).catch(failure).finally(() => { polling = null; });
    return polling;
  }
  async function poll() {
    await refreshStatus();
    if (!expired) setTimeout(poll, 2000);
  }
  async function mutate(action) {
    if (busy || expired) return;
    busy = true; controls(); $("error").hidden = true; show("message", "Working… Conflicting controls are disabled.");
    try { const result = await action(); show("message", typeof result === "string" ? result : result === false ? "Action cancelled." : "Command completed."); }
    catch (error) { failure(error); show("message", "Command failed. Reload status before retrying."); }
    finally { await refreshStatus(); busy = false; controls(); }
  }
  async function confirm(text) {
    show("confirmation-text", text);
    const dialog = $("confirmation"); dialog.returnValue = "cancel"; dialog.showModal();
    return new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true}));
  }
  function selectedSite() { return $("site-select").value; }
  function selectedMetadata() {
    const site = sites.find(s => s.site_id === selectedSite());
    for (const name of ["address", "description"]) $("edit-site").elements[name].value = site?.[name] || "";
    show("registration-site", `Selected site UUID: ${selectedSite() || "None"}`);
    clearRegistration();
  }
  async function loadSites(preferred) {
    const page = await api(`/sites?offset=${siteOffset}&limit=${pageSize}`);
    siteTotal = page.total; sites = page.items;
    const previous = preferred || selectedSite() || status?.logger.site_id;
    $("site-select").replaceChildren();
    for (const s of sites) { const option = document.createElement("option"); option.value = s.site_id; option.textContent = `${s.address || "Unnamed site"} — ${s.site_id}`; $("site-select").append(option); }
    if (sites.some(s => s.site_id === previous)) $("site-select").value = previous;
    selectedMetadata(); show("site-page", `${siteTotal ? siteOffset + 1 : 0}–${siteOffset + sites.length} of ${siteTotal} sites`);
    $("sites-prev").disabled = siteOffset === 0; $("sites-next").disabled = siteOffset + pageSize >= siteTotal;
  }
  async function loadSessions() {
    if (sessionsLoading) return;
    sessionsLoading = true;
    const requestedOffset = sessionOffset;
    let reloadRequestedPage = false;
    try {
    const page = await api(`/sessions?offset=${requestedOffset}&limit=${pageSize}`);
    // A pagination click can change sessionOffset while this request is pending.
    // Never render old rows with the newer page's range; fetch that requested page.
    if (requestedOffset !== sessionOffset) {
      reloadRequestedPage = true;
      return;
    }
    const checked = new Set([...$("sessions").querySelectorAll("input:checked")].map(i => i.value));
    const focused = document.activeElement?.matches("#sessions input[type=checkbox]")
      ? document.activeElement.value : null;
    sessionTotal = page.total; $("sessions").replaceChildren();
    for (const s of page.items) {
      const row = document.createElement("tr"), choice = document.createElement("td"), input = document.createElement("input");
      input.type = "checkbox"; input.value = s.session_id; input.setAttribute("aria-label", `Clean session ${s.session_id}`);
      input.disabled = s.state !== "completed" || s.eligible_uploaded_count === 0; choice.append(input); row.append(choice);
      input.checked = checked.has(s.session_id);
      for (const text of [`${s.session_id}\nSource: ${s.source_id}\nSite: ${s.site_id}`, `${s.state} / ${s.collection_enabled ? "enabled" : "paused"}`, s.total_count, s.pending_count, s.eligible_uploaded_count, s.upload_error ? `${s.upload_error}: ${explainError(s.upload_error)}` : "None"]) {
        const cell = document.createElement("td"); cell.textContent = text; row.append(cell);
      }
      $("sessions").append(row);
    }
    if (focused) $("sessions").querySelector(`input[value="${CSS.escape(focused)}"]`)?.focus();
    show("session-page", `${sessionTotal ? requestedOffset + 1 : 0}–${requestedOffset + page.items.length} of ${sessionTotal} sessions`);
    $("sessions-prev").disabled = sessionOffset === 0; $("sessions-next").disabled = sessionOffset + pageSize >= sessionTotal;
    } finally {
      sessionsLoading = false;
      if (reloadRequestedPage && section === "maintenance") loadSessions().catch(failure);
    }
  }
  document.querySelectorAll("[data-section]").forEach(button => button.onclick = () => {
    clearRegistration(); section = button.dataset.section;
    document.querySelectorAll("main > section").forEach(s => s.hidden = s.id !== section);
    if (section === "maintenance") loadSessions().catch(failure);
    if (section === "settings") loadSites().catch(failure);
  });
  $("site-select").onchange = selectedMetadata;
  $("logout").onsubmit = clearRegistration;
  window.addEventListener("pagehide", clearRegistration);
  $("reload").onclick = refreshStatus;
  $("retry-upload").onclick = async () => {
    if (expired || !status || retrying) return;
    retrying = true; $("retry-upload").disabled = true;
    $("error").hidden = true;
    show("message", "Retry requested. Delivery will be confirmed only after the Server accepts records and the checkpoint is saved.");
    try { renderStatus(await api("/upload/retry", "POST", {})); }
    catch (error) { failure(error); }
    finally { retrying = false; $("retry-upload").disabled = expired || !status; }
  };
  for (const action of ["start", "pause", "complete"]) $(action).onclick = () => mutate(async () => {
    const id = status.logger.session_id;
    if (action === "complete" && !await confirm(`Complete session ${id}, source ${status.logger.source_id}? Collection stops and pending records remain protected. A new deployment is needed to collect again.`)) return false;
    renderStatus(await api(`/sessions/${id}/${action}`, "POST", {}));
  });
  $("deploy").onclick = () => mutate(async () => {
    const target = selectedSite(), current = status.logger.session_id;
    if (!target) throw {code: "no_site", message: "Create or select a site first."};
    const switching = status.active_session_state === "open";
    if (switching && !await confirm(`Switch from site ${status.logger.site_id}, session ${current}, source ${status.logger.source_id} to site ${target}? The old session completes after draining; its records remain pending. A new paused session/source is created.`)) return false;
    renderStatus(await api("/deployment", "POST", {site_id: target, expected_session_id: current, confirm_switch: switching}));
  });
  $("create-site").onsubmit = event => { event.preventDefault(); mutate(async () => {
    const form = event.target.elements, body = {address: form.address.value, description: form.description.value};
    if (form.site_id.value.trim()) body.site_id = form.site_id.value.trim();
    const site = await api("/sites", "POST", body); siteOffset = 0; await loadSites(site.site_id);
    // Imported UUIDs may be on another page. Find them using bounded pages.
    while (!sites.some(s => s.site_id === site.site_id) && siteOffset + pageSize < siteTotal) { siteOffset += pageSize; await loadSites(site.site_id); }
  }); };
  $("edit-site").onsubmit = event => { event.preventDefault(); mutate(async () => {
    if (!selectedSite()) throw {code: "no_site", message: "Select a saved site."};
    await api(`/sites/${selectedSite()}`, "PATCH", {address: event.target.elements.address.value, description: event.target.elements.description.value}); await loadSites();
  }); };
  $("config").onsubmit = event => { event.preventDefault(); mutate(async () => {
    const f = event.target.elements;
    await api("/config", "PATCH", {expected_session_id: status.logger.session_id, server_url: f.server_url.value.trim() || null, serial_path: f.serial_path.value.trim() || null, baudrate: Number(f.baudrate.value), bytesize: Number(f.bytesize.value), parity: f.parity.value, stopbits: f.stopbits.value});
  }); };
  $("preview").onclick = () => mutate(async () => { previewReading = await api("/preview", "POST", {}); renderReading(previewReading, "Read preview (not stored)"); });
  $("meter-confirm").onclick = () => mutate(async () => {
    const id = status.logger.session_id;
    if (!await confirm(`Replace the meter binding for session ${id}, source ${status.logger.source_id}? This completes the old session and creates a new paused session/source. Any storage block remains.`)) return false;
    renderStatus(await api("/meter-change/confirm", "POST", {expected_session_id: id}));
  });
  $("storage-resolve").onclick = () => mutate(async () => { renderStatus(await api("/storage/resolve", "POST", {})); });
  $("reveal").onclick = () => mutate(async () => {
    clearRegistration();
    const generation = registrationGeneration;
    const target = selectedSite();
    if (!target) throw {code: "no_site", message: "Select a saved site in Site and settings."};
    const result = await api("/registration-summary", "POST", {site_id: target});
    if (section !== "registration" || expired || generation !== registrationGeneration) return;
    $("registration-text").value = JSON.stringify({site_id: result.site_id, address: result.address,
      description: result.description, device_id: result.device_id, token: result.token}, null, 2);
    details("registration-display", [["Site UUID", result.site_id], ["Address", result.address],
      ["Description", result.description], ["Device UUID", result.device_id], ["Token", result.token]]);
    $("registration-label").hidden = $("copy").hidden = false;
    show("copy-help", "If clipboard access is unavailable, select this text and copy it manually.");
  });
  $("copy").onclick = async () => {
    try { await navigator.clipboard.writeText($("registration-text").value); show("copy-help", "Registration information copied."); }
    catch { $("registration-text").focus(); $("registration-text").select(); show("copy-help", "Clipboard unavailable. Text selected; press Ctrl+C or Command+C to copy."); }
  };
  $("cleanup").onclick = () => mutate(async () => {
    const ids = [...$("sessions").querySelectorAll("input:checked")].map(i => i.value);
    if (!ids.length) throw {code: "no_selection", message: "Select eligible completed sessions first."};
    if (!await confirm(`Delete only uploaded records from completed sessions ${ids.join(", ")}? Pending records and session metadata remain. This deletion cannot be undone.`)) return false;
    const result = await api("/cleanup", "POST", {session_ids: ids, confirm: true});
    await loadSessions(); return `Deleted ${result.deleted_count} uploaded records; retained ${result.retained_pending_count} pending records; free bytes ${result.free_bytes}.`;
  });
  for (const [kind, load] of [["sites", loadSites], ["sessions", loadSessions]]) for (const [direction, delta] of [["prev", -pageSize], ["next", pageSize]]) $( `${kind}-${direction}` ).onclick = () => {
    if (kind === "sites") siteOffset += delta; else sessionOffset += delta;
    load().catch(failure);
  };
  async function initialize() {
    await refreshStatus();
    if (expired) return;
    try { const config = await api("/config"); for (const name of ["server_url", "serial_path", "baudrate", "bytesize", "parity", "stopbits"]) $("config").elements[name].value = config[name] ?? ""; await loadSites(); }
    catch (error) { failure(error); }
    setTimeout(poll, 2000);
  }
  initialize();
})();
