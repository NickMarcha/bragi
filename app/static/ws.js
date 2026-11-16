// Realtime control plane: one WebSocket per tab. Continuous slider drags
// are throttled here (client-side) and again server-side (see app/ws.py) -
// this throttle mainly avoids flooding the socket itself; the server-side
// one is what actually protects PipeWire from a subprocess-per-tick.
(() => {
  const THROTTLE_MS = 80;
  let socket = null;
  let reconnectDelay = 500;
  let displayMode = "percent"; // or "db" - see wireUnitToggle()

  // Per-control "ts of the last message *I* sent" - an incoming broadcast
  // is only displayed once it catches up to this, never compared against
  // whatever the last-*displayed*-broadcast happened to be. That distinction
  // is why an earlier version of this still visibly jumped backward after
  // releasing a drag: broadcasts keep arriving throughout a fast drag
  // (each reflecting some earlier position, since the server lags behind),
  // and the dragging-flag guard below correctly skips *displaying* them -
  // but a "last shown" tracker still silently recorded each one's ts as
  // "seen", which is a much lower bar than "caught up to my true final
  // position". The moment you release, the *next* arriving broadcast only
  // had to beat that stale bookmark, not your actual last input, so it
  // displayed some intermediate value and you watched it crawl the rest of
  // the way. Comparing against what THIS CLIENT last sent instead closes
  // that gap: every broadcast older than your own last input is invisible,
  // full stop, regardless of what's been displayed in the meantime.
  const lastSentTs = new Map();

  function tsKeyFor(target, key, direction) {
    return target + ":" + key + ":" + direction;
  }

  // A pure Date.now() has only ms resolution - two messages fired close
  // together (a fast drag, or two different controls) can land in the
  // same millisecond, and the server's ts check rejects *ties* (not just
  // older values, see ws.py's _accept_ts), so a tied-but-different-value
  // message would be wrongly dropped. Confirmed live: an occasional
  // permanent (non-self-correcting) stuck value during a fast drag. This
  // tie-breaker keeps every ts strictly increasing while staying numerically
  // close to Date.now() (a tiny fractional nudge, well under 1ms).
  let tsCounter = 0;
  function nextTs() {
    tsCounter += 1;
    return Date.now() + tsCounter / 1e6;
  }

  function cssEscape(s) {
    return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
  }

  function setStatus(state) {
    const el = document.getElementById("ws-status");
    if (!el) return;
    el.className = state;
    const label = el.querySelector(".label");
    if (label) {
      label.textContent = state === "connected" ? "Connected" : state === "connecting" ? "Connecting…" : "Disconnected";
    }
  }

  // An expired access cookie cannot be refreshed by a WebSocket handshake
  // (it cannot set cookies), so ask an ordinary endpoint first: reaching
  // it refreshes the cookie, and a 401 means this browser has to log in.
  async function connect() {
    setStatus("connecting");
    try {
      const response = await fetch("/auth/session", { credentials: "same-origin" });
      if (response.status === 401) {
        location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        return;
      }
    } catch (_) {
      setStatus("disconnected");
      scheduleReconnect();
      return;
    }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${proto}//${location.host}/ws`);
    socket.addEventListener("open", () => {
      reconnectDelay = 500;
      setStatus("connected");
    });
    socket.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "state") applyState(msg);
      else if (msg.type === "control") applyControl(msg);
      else if (msg.type === "headset") applyHeadset(msg);
      else if (msg.type === "headset_controls") applyHeadsetControls(msg.key, msg.controls);
      else if (msg.type === "peer_presence") applyPeerPresence(msg);
      else if (msg.type === "peer_streams") applyPeerStreams(msg);
      else if (msg.type === "peer_status") applyPeerStatus(msg);
      else if (msg.type === "peer_registry") {
        htmx.ajax("GET", "/peers", { target: "#peers", swap: "innerHTML" });
        htmx.ajax("GET", "/audio/microphone", { target: "#microphone-routing", swap: "innerHTML" });
      }
      else if (msg.type === "microphone") htmx.ajax("GET", "/audio/microphone", { target: "#microphone-routing", swap: "innerHTML" });
      else if (msg.type === "bluetooth" || msg.type === "gamepads") refreshBluetooth();
      else if (msg.type === "hello") {
        // Bragi restarted on another version (an update): this page is the old one's.
        const page = document.body.dataset.version;
        if (page && msg.version && page !== msg.version) location.reload();
      }
      else if (msg.type === "updates" && document.getElementById("updates")) {
        htmx.ajax("GET", "/updates", { target: "#updates", swap: "innerHTML" });
      }
      else if (msg.type === "kvm") htmx.ajax("GET", "/kvm", { target: "#kvm-bar", swap: "innerHTML" });
      else if (msg.type === "levels") applyLevels(msg);
      else if (msg.type === "viz_settings") applyVizSettings(msg);
      else if (msg.type === "nightly_reset") applyNightlyReset(msg);
      else if (msg.type === "eq_presets") applyEqPresets(msg.presets);
      else if (msg.type === "order") applyOrder(msg.order);
    });
    socket.addEventListener("close", () => {
      setStatus("disconnected");
      scheduleReconnect();
    });
    socket.addEventListener("error", () => socket.close());
  }

  function scheduleReconnect() {
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 8000);
  }

  function send(action) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(action));
    }
  }

  // --- numeric readout formatting (% by default, dB via the top-bar toggle) ---

  function formatVolume(volume) {
    if (volume == null) return "--";
    // No " dB" suffix: the readout sits on a fader thumb, and the unit
    // toggle in the toolbar already says which unit is showing.
    if (displayMode === "db") {
      if (volume <= 0.0001) return "-\u221e";
      return (20 * Math.log10(volume)).toFixed(1);
    }
    return `${Math.round(volume * 100)}%`;
  }

  function setReadout(strip, volume) {
    const readout = strip && strip.querySelector(".readout");
    if (readout && volume != null) readout.textContent = formatVolume(volume);
  }

  function refreshAllReadouts() {
    document.querySelectorAll('.fader[data-action="volume"]').forEach((fader) => {
      setReadout(fader.closest(".strip"), parseFloat(fader.dataset.value));
    });
  }

  function wireUnitToggle() {
    const btn = document.getElementById("unit-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      displayMode = displayMode === "percent" ? "db" : "percent";
      btn.textContent = displayMode === "percent" ? "%" : "dB";
      refreshAllReadouts();
    });
  }

  function wireSettingsDialog() {
    const toggleBtn = document.getElementById("settings-toggle");
    const dialog = document.getElementById("viz-settings-dialog");
    const closeBtn = document.getElementById("viz-settings-close");
    const checkbox = document.getElementById("viz-enabled-checkbox");
    if (!toggleBtn || !dialog) return;
    toggleBtn.addEventListener("click", () => dialog.showModal());
    if (closeBtn) closeBtn.addEventListener("click", () => dialog.close());
    if (checkbox) {
      checkbox.addEventListener("change", () => {
        send({ action: "set_viz_enabled", value: checkbox.checked });
      });
    }
    const nightly = document.getElementById("nightly-reset-select");
    if (nightly) {
      nightly.addEventListener("change", () => {
        send({ action: "set_nightly_reset", value: nightly.value === "off" ? null : Number(nightly.value) });
      });
    }
  }

  // Closes on success; shows the server's reason on failure (htmx does not
  // swap error responses, so without this a rejected add looked like nothing).
  // The Bluetooth dialog (app/templates/_bluetooth.html): its body is
  // server-rendered and reloaded on "bluetooth"/"gamepads" broadcasts, but
  // only while the dialog is open.
  function refreshBluetooth() {
    const dialog = document.getElementById("bluetooth-dialog");
    if (dialog && dialog.open) htmx.ajax("GET", "/bluetooth", { target: "#bluetooth-body", swap: "innerHTML" });
  }

  function wireBluetoothDialog() {
    const dialog = document.getElementById("bluetooth-dialog");
    const openBtn = document.getElementById("bluetooth-toggle");
    if (!dialog || !openBtn) return;
    openBtn.addEventListener("click", () => {
      dialog.showModal();
      refreshBluetooth();
    });
    document.getElementById("bluetooth-close").addEventListener("click", () => dialog.close());
  }

  function wireAddPeerDialog() {
    const dialog = document.getElementById("add-peer-dialog");
    const form = document.getElementById("add-peer");
    const openBtn = document.getElementById("add-peer-toggle");
    if (!dialog || !form || !openBtn) return;
    const error = form.querySelector(".form-error");
    openBtn.addEventListener("click", () => {
      error.hidden = true;
      dialog.showModal();
    });
    document.getElementById("add-peer-cancel").addEventListener("click", () => dialog.close());
    form.addEventListener("htmx:afterRequest", (evt) => {
      if (evt.detail.successful) {
        form.reset();
        dialog.close();
        return;
      }
      let message = "Could not add the peer.";
      try {
        message = JSON.parse(evt.detail.xhr.responseText).detail || message;
      } catch (_) { /* not JSON */ }
      error.textContent = message;
      error.hidden = false;
    });
  }

  // --- device order and the parked stack ---
  //
  // Devices are ordered with the CSS order property, not by moving them:
  // headsets and peers live in different containers (#peers is the htmx
  // swap target, display: contents), and order works across both. The
  // order is saved on the Pi (app/layout.py) and broadcast to every tab.

  let draggingCard = null;

  function deviceId(card) {
    return card.dataset.headset ? `headset:${card.dataset.headset}` : `peer:${card.dataset.peer}`;
  }

  function rowDevices() {
    return Array.from(document.querySelectorAll("#console .device"))
      .filter((card) => !card.closest("#parked"))
      .sort((a, b) => Number(a.style.order) - Number(b.style.order));
  }

  function applyOrder(order) {
    if (draggingCard || !Array.isArray(order)) return;
    const saved = new Map(order.map((id, i) => [id, i]));
    const devices = Array.from(document.querySelectorAll("#console .device"));
    devices.sort((a, b) =>
      (saved.get(deviceId(a)) ?? Infinity) - (saved.get(deviceId(b)) ?? Infinity)
      || Number(a.style.order) - Number(b.style.order));
    devices.forEach((card, i) => { card.style.order = i; });
  }

  // A switched-off headset, or a peer with neither direction in the graph
  // (a phone whose app isn't running), has nothing to mix: it leaves the
  // row for the stack at the end (#parked, inside #peers) and returns to
  // its old place when it comes back. _peers_list.html makes the same call
  // on first render.
  function shouldPark(card) {
    if (card.dataset.headset) return card.classList.contains("headset-disabled");
    // Same rule as app/views.py's peer_view "parked": a peer with a client
    // (phone app, desktop tray app) parks while it is away.
    if (card.dataset.clientTracked === "true") return card.dataset.clientConnected !== "true";
    const offline = card.querySelectorAll(".strip .offline");
    return offline.length > 0 && Array.from(offline).every((el) => !el.hidden);
  }

  function placeDevice(card) {
    const parked = document.getElementById("parked");
    if (!parked || card === draggingCard) return;
    if (shouldPark(card)) {
      if (card.parentElement === parked) return;
      parked.appendChild(card);
    } else if (card.parentElement === parked) {
      if (card.dataset.headset) {
        document.getElementById("console").insertBefore(card, document.getElementById("peers"));
      } else {
        document.getElementById("peers").insertBefore(card, parked);
      }
    } else {
      return;
    }
    scheduleRowCount();
  }

  // Devices wrap onto more rows when the window is too narrow for one (half
  // a 1080p screen, say). --rows lets style.css shrink the faders so every
  // row still fits the window's height.
  let rowCountPending = false;
  function scheduleRowCount() {
    if (rowCountPending) return;
    rowCountPending = true;
    requestAnimationFrame(() => {
      rowCountPending = false;
      const tops = new Set();
      for (const card of rowDevices()) tops.add(card.offsetTop);
      const parked = document.getElementById("parked");
      if (parked && parked.querySelector(".device")) tops.add(parked.offsetTop);
      const rows = String(Math.max(1, tops.size));
      if (document.documentElement.style.getPropertyValue("--rows") !== rows) {
        document.documentElement.style.setProperty("--rows", rows);
      }
    });
  }

  // Dragging the grip moves a device along the row (or down the column on
  // a phone). Wired once on the document, so cards htmx swaps in later
  // work without rewiring.
  function wireReorder() {
    let devices = [];
    let vertical = false;
    document.addEventListener("pointerdown", (evt) => {
      const grip = evt.target.closest && evt.target.closest(".grip");
      if (!grip || grip.closest("#parked")) return;
      evt.preventDefault();
      draggingCard = grip.closest(".device");
      devices = rowDevices();
      vertical = getComputedStyle(document.getElementById("console")).flexDirection === "column";
      grip.setPointerCapture(evt.pointerId);
      draggingCard.classList.add("dragging");
    });
    document.addEventListener("pointermove", (evt) => {
      if (!draggingCard) return;
      const pointer = vertical ? evt.clientY : evt.clientX;
      const others = devices.filter((card) => card !== draggingCard);
      let index = 0;
      for (const card of others) {
        const r = card.getBoundingClientRect();
        if (pointer > (vertical ? r.top + r.height / 2 : r.left + r.width / 2)) index++;
      }
      others.splice(index, 0, draggingCard);
      others.forEach((card, i) => { card.style.order = i; });
      devices = others;
    });
    const drop = () => {
      if (!draggingCard) return;
      draggingCard.classList.remove("dragging");
      draggingCard = null;
      const parkedIds = Array.from(document.querySelectorAll("#parked .device")).map(deviceId);
      send({ action: "set_order", value: devices.map(deviceId).concat(parkedIds) });
    };
    document.addEventListener("pointerup", drop);
    document.addEventListener("pointercancel", drop);
  }

  // --- applying server state ---

  function applyState(state) {
    for (const view of state.headsets || []) {
      applyHeadset(view);
    }
    for (const view of state.peers || []) {
      applyCard("peer", view.name, [
        ["outgoing", view.outgoing],
        ["incoming", view.incoming],
      ]);
    }
    if (state.viz_settings) applyVizSettings(state.viz_settings);
    if (state.nightly_reset) applyNightlyReset(state.nightly_reset);
    if (state.eq_presets) applyEqPresets(state.eq_presets);
  }

  // One frame per ~50ms audio chunk from app/level_meter.py, carrying every
  // metered direction at once (it used to be one frame per direction, which
  // at 8 directions x 20Hz was 160 frames a second for one open dashboard).
  // Paints the live-signal bar next to the fader, never the fader itself
  // (see _fader.html's level-meter comment for why that's a deliberately
  // separate element from the volume fill).
  //
  // The .level-fill elements are looked up once and cached: at 20Hz across
  // 8 meters, re-running two document queries per meter per frame is 320
  // needless DOM searches a second, on a page whose strips never move.
  const levelFills = new Map();

  function levelFillFor(target, key, direction) {
    const cacheKey = tsKeyFor(target, key, direction);
    if (levelFills.has(cacheKey)) return levelFills.get(cacheKey);
    const attr = target === "headset" ? "data-headset" : "data-peer";
    const card = document.querySelector(`[${attr}="${cssEscape(key)}"]`);
    const section = card && card.querySelector(`.strip[data-direction="${direction}"]`);
    const fill = section ? section.querySelector(".level-fill") : null;
    levelFills.set(cacheKey, fill);
    return fill;
  }

  function applyLevels(msg) {
    for (const level of msg.values || []) {
      const fill = levelFillFor(level.target, level.key, level.direction);
      if (fill) fill.style.setProperty("--level", Math.max(0, Math.min(1, level.value)));
    }
  }

  // The one place viz_settings.enabled (see app/viz_settings.py) turns
  // into UI state - toggles the body class that CSS gates every
  // .level-meter's visibility on, and keeps the settings checkbox in sync
  // whether the change came from this tab or another one.
  function applyVizSettings(msg) {
    document.body.classList.toggle("viz-enabled", !!msg.enabled);
    const checkbox = document.getElementById("viz-enabled-checkbox");
    if (checkbox) checkbox.checked = !!msg.enabled;
  }

  // Keeps every tab's settings dialog on the same nightly reset hour.
  function applyNightlyReset(msg) {
    const select = document.getElementById("nightly-reset-select");
    if (select) select.value = msg.hour === null || msg.hour === undefined ? "off" : String(msg.hour);
  }

  // Targeted update for a single control (the common case - every slider
  // drag tick and every hardware-knob-driven watcher push arrives as one
  // of these, not a full applyState() snapshot, which is reserved for the
  // initial connect - see app/ws.py's module docstring for why.
  function applyControl(msg) {
    const attr = msg.target === "headset" ? "data-headset" : "data-peer";
    const card = document.querySelector(`[${attr}="${cssEscape(msg.key)}"]`);
    if (!card) return;
    applyDirection(card, msg.direction, msg);
  }

  // Whole-headset refresh after enable/disable - not a per-direction
  // control (it's a card.profile change), so it carries its own
  // playback/capture sub-views rather than reusing applyControl's shape.
  function applyHeadset(msg) {
    const card = document.querySelector(`[data-headset="${cssEscape(msg.key)}"]`);
    if (!card) return;
    card.classList.toggle("headset-disabled", !msg.enabled);
    placeDevice(card);
    const toggleBtn = card.querySelector('[data-action="toggle_enabled"]');
    if (toggleBtn) toggleBtn.title = msg.enabled ? "Disable headset" : "Enable headset";
    if (msg.playback) applyDirection(card, "playback", msg.playback);
    if (msg.capture) applyDirection(card, "capture", msg.capture);
    if ("controls" in msg) applyHeadsetControls(msg.key, msg.controls);
  }

  // --- HyperX controls (app/headset_hid.py, _headset_controls.html) ---
  // Battery in the card's meta line, the rest in the card's own dialog.
  // controls is null for a headset without one of these dongles, which
  // hides all of it.

  // Same wording as _headset_controls.html's battery_text and battery_short.
  function batteryText(c) {
    if (!c) return "";
    if (c.headset_on === false) return "Headset off";
    if (c.battery == null) return "";
    const suffix = { charging: ", charging", full: ", charged" }[c.charging] || "";
    return `Battery ${c.battery}%${suffix}`;
  }

  function batteryShort(c) {
    if (!c) return "";
    if (c.headset_on === false) return "Off";
    return c.battery == null ? "" : `${c.battery}%`;
  }

  function formatGain(gain) {
    return gain > 0 ? `+${gain}` : String(gain);
  }

  // EQ presets (app/eq_presets.py): the list every tab shares, and the
  // last controls each headset was sent, so a changed list can redraw the
  // picker without waiting for the server.
  let eqPresets = [];
  const lastControls = new Map();

  // Same matching as eq_presets.matching(): the first preset with exactly
  // these gains. Used while dragging, before the server has heard of it.
  function matchingPreset(gains) {
    if (!gains) return null;
    const hit = eqPresets.find((p) => p.gains.every((g, i) => Number(g) === Number(gains[i])));
    return hit ? hit.name : null;
  }

  function applyEqPresets(presets) {
    eqPresets = presets;
    document.querySelectorAll("[data-hid-preset]").forEach((select) => {
      const saved = select.querySelector('optgroup[label="Saved"]');
      saved.replaceChildren(...presets.filter((p) => !p.builtin).map((p) => new Option(p.name, p.name)));
      saved.hidden = !saved.children.length;
    });
    for (const [key, c] of lastControls) applyHeadsetControls(key, c);
  }

  // The picker, its description line and the delete button, for the given
  // gains and the preset they match. Mirrors _headset_controls.html.
  function showPreset(dialog, gains, active) {
    const select = dialog.querySelector("[data-hid-preset]");
    const placeholder = select.querySelector('option[value=""]');
    placeholder.textContent = gains ? "Custom" : "Unknown";
    select.value = active || "";
    const preset = eqPresets.find((p) => p.name === active);
    let description = "";
    if (preset) description = preset.description || "Saved preset.";
    else if (gains) description = "Your own settings. Save them as a preset to come back to them.";
    dialog.querySelector('[data-hid="preset-description"]').textContent = description;
    dialog.querySelector("[data-hid-preset-delete]").disabled = !(preset && !preset.builtin);
  }

  // --- The macro button (app/macro_button.py) ---

  // "Name: value" per line, as the form shows headers. Same as the
  // template's join.
  function headersText(headers) {
    return Object.entries(headers || {}).map(([name, value]) => `${name}: ${value}`).join("\n");
  }

  function macroResultText(result) {
    if (!result) return "Not run since Bragi started";
    const at = new Date(result.at * 1000).toLocaleTimeString();
    return `Last: ${result.text}, ${at}`;
  }

  // [the gesture's action, null] from its fields, or [null, what's wrong
  // with them]. The server checks the same (macro_button.valid_action) and
  // ignores an action that fails, so a gesture with an error isn't sent.
  function macroAction(row) {
    const field = (name) => row.querySelector(`[data-macro-field="${name}"]`).value;
    const type = field("type");
    if (type !== "http") return [{ type }, null];
    const action = {
      type, method: field("method"), target: field("target"), url: field("url").trim(),
      socket: field("socket").trim(), path: field("path").trim() || "/", headers: {}, body: field("body").trim(),
    };
    if (action.target === "url" && !/^https?:\/\/[^/\s?#]+/.test(action.url)) {
      return [null, "the URL starts with http:// or https:// and names a host"];
    }
    if (action.target === "socket") {
      if (!action.socket.startsWith("/")) return [null, "the socket is an absolute path, like /run/user/1000/app.sock"];
      if (!action.path.startsWith("/") || /\s/.test(action.path)) return [null, "the HTTP path starts with / and has no spaces"];
    }
    for (const line of field("headers").split("\n")) {
      if (!line.trim()) continue;
      const colon = line.indexOf(":");
      const name = line.slice(0, colon).trim();
      if (colon < 1 || !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(name)) return [null, `"${line.trim()}" isn't a Name: value header`];
      action.headers[name] = line.slice(colon + 1).trim();
    }
    if (action.body) {
      try {
        JSON.parse(action.body);
      } catch (err) {
        return [null, "the body isn't valid JSON"];
      }
    }
    return [action, null];
  }

  function showMacroResult(row, text, failed) {
    const note = row.querySelector("[data-macro-result]");
    note.textContent = text;
    note.classList.toggle("macro-failed", failed);
  }

  function applyMacro(dialog, macro) {
    dialog.querySelectorAll("[data-macro]").forEach((row) => {
      // A gesture holding an edit that couldn't be saved keeps it, and
      // says why, until it's fixed.
      if (row.dataset.unsaved === "1") return;
      const g = (macro || {})[row.dataset.macro] || { action: { type: "none" }, result: null };
      showMacroResult(row, macroResultText(g.result), !!g.result && !g.result.ok);
      // The fields stay as they are while you're in them (Test keeps focus).
      if (row.contains(document.activeElement)) return;
      const a = g.action;
      const values = {
        type: a.type, method: a.method || "POST", target: a.target || "url", url: a.url || "",
        socket: a.socket || "", path: a.path || "/", headers: headersText(a.headers), body: a.body || "",
      };
      for (const [name, value] of Object.entries(values)) {
        row.querySelector(`[data-macro-field="${name}"]`).value = value;
      }
      row.querySelector("[data-macro-test]").disabled = a.type === "none";
    });
  }

  function wireMacro(dialog, key) {
    dialog.querySelectorAll("[data-macro]").forEach((row) => {
      const gesture = row.dataset.macro;
      const test = row.querySelector("[data-macro-test]");
      row.querySelectorAll("[data-macro-field]").forEach((input) => {
        input.addEventListener("change", () => {
          test.disabled = row.querySelector('[data-macro-field="type"]').value === "none";
          const [action, error] = macroAction(row);
          if (error) {
            row.dataset.unsaved = "1";
            showMacroResult(row, `Not saved: ${error}`, true);
            return;
          }
          row.dataset.unsaved = "0";
          send({ action: "set_macro", target: "headset", key, value: { gesture, action } });
        });
      });
      test.addEventListener("click", () => {
        const [action, error] = macroAction(row);
        if (error) {
          showMacroResult(row, `Can't run: ${error}`, true);
          return;
        }
        showMacroResult(row, "Running...", false);
        send({ action: "test_macro", target: "headset", key, value: { gesture, action } });
      });
    });
  }

  function applyHeadsetControls(key, c) {
    if (c) lastControls.set(key, c);
    else lastControls.delete(key);
    const card = document.querySelector(`[data-headset="${cssEscape(key)}"]`);
    if (!card) return;
    const text = batteryText(c);
    const short = batteryShort(c);
    const battery = card.querySelector('[data-hid="battery"]');
    if (battery) {
      battery.hidden = !short;
      battery.textContent = short;
      battery.classList.toggle("charging", !!c && c.charging === "charging");
      card.querySelector('[data-hid="meta-default"]').hidden = !!short;
      battery.closest(".device-meta").title = text || "USB headset";
    }
    const openBtn = card.querySelector("[data-hid-open]");
    if (openBtn) openBtn.hidden = !c;

    const dialog = card.querySelector("[data-hid-dialog]");
    if (!dialog || !c) {
      if (dialog && dialog.open) dialog.close();
      return;
    }
    const off = c.headset_on === false;
    dialog.querySelector('[data-hid="status"]').textContent = text || "Waiting for the headset";

    const monitor = dialog.querySelector('[data-hid-action="set_mic_monitor"]');
    monitor.disabled = off;
    monitor.indeterminate = c.mic_monitor == null;
    monitor.checked = !!c.mic_monitor;

    // Bragi's loopback (app/mic_monitor.py): not tied to the headset
    // being on, so never disabled with it.
    const m = c.monitor || { enabled: false, volume: 1 };
    dialog.querySelector('[data-monitor="enabled"]').checked = m.enabled;
    const monitorVolume = dialog.querySelector('[data-monitor="volume"]');
    if (monitorVolume.dataset.dragging !== "1") {
      monitorVolume.value = Math.round(m.volume * 100);
      dialog.querySelector('[data-monitor="readout"]').textContent = `${Math.round(m.volume * 100)}%`;
    }

    const autoOff = dialog.querySelector('[data-hid-action="set_auto_off"]');
    autoOff.disabled = off;
    let wanted = "";
    if (c.auto_off_known) wanted = c.auto_off_minutes == null ? "never" : String(c.auto_off_minutes);
    if (wanted && !autoOff.querySelector(`option[value="${wanted}"]`)) {
      // A value set elsewhere that isn't one of Bragi's choices.
      autoOff.add(new Option(`${wanted} min idle`, wanted));
    }
    autoOff.value = wanted;

    dialog.querySelector(".hid-eq").disabled = off;
    dialog.querySelector("[data-hid-flat]").disabled = off;
    dialog.querySelectorAll("[data-eq-band]").forEach((input) => {
      if (input.dataset.dragging === "1") return;
      const gain = c.eq ? c.eq[Number(input.dataset.eqBand)] : 0;
      input.value = gain;
      input.closest(".hid-eq-band").querySelector("[data-eq-readout]").textContent = formatGain(gain);
    });
    const select = dialog.querySelector("[data-hid-preset]");
    select.disabled = off;
    dialog.querySelector('.hid-preset-save button[type="submit"]').disabled = off;
    showPreset(dialog, c.eq, c.eq_preset);
    dialog.querySelector('[data-hid="eq-note"]').textContent = c.eq
      ? "The headset can't report its EQ. This is what Bragi last set."
      : "The headset can't report its EQ, and Bragi hasn't set one yet, so these sliders don't show what it's using now.";

    applyMacro(dialog, c.macro);
  }

  function wireHeadsetControls(root) {
    root.querySelectorAll("[data-hid-dialog]").forEach((dialog) => {
      const key = dialog.dataset.hidDialog;
      const card = dialog.closest(".device");
      const openBtn = card && card.querySelector("[data-hid-open]");
      if (openBtn) openBtn.addEventListener("click", () => dialog.showModal());
      dialog.querySelector("[data-hid-close]").addEventListener("click", () => dialog.close());
      wireMacro(dialog, key);

      const monitor = dialog.querySelector('[data-hid-action="set_mic_monitor"]');
      monitor.addEventListener("change", () => {
        send({ action: "set_mic_monitor", target: "headset", key, value: monitor.checked });
      });

      const monitorOn = dialog.querySelector('[data-monitor="enabled"]');
      monitorOn.addEventListener("change", () => {
        send({ action: "set_monitor", target: "headset", key, value: monitorOn.checked });
      });
      const monitorVolume = dialog.querySelector('[data-monitor="volume"]');
      const monitorReadout = dialog.querySelector('[data-monitor="readout"]');
      const sendMonitorVolume = () =>
        send({ action: "set_monitor_volume", target: "headset", key, value: Number(monitorVolume.value) / 100 });
      // Sent while dragging, so you hear the change as you go: the server
      // puts each step straight on the running loopback.
      const sendMonitorVolumeThrottled = throttle(sendMonitorVolume, 100);
      monitorVolume.addEventListener("input", () => {
        monitorVolume.dataset.dragging = "1";
        monitorReadout.textContent = `${monitorVolume.value}%`;
        sendMonitorVolumeThrottled();
      });
      monitorVolume.addEventListener("change", () => {
        monitorVolume.dataset.dragging = "0";
        sendMonitorVolume();
      });
      // Right-click back to 100%, like the faders.
      monitorVolume.addEventListener("contextmenu", (evt) => {
        evt.preventDefault();
        monitorVolume.value = 100;
        monitorReadout.textContent = "100%";
        sendMonitorVolume();
      });

      const autoOff = dialog.querySelector('[data-hid-action="set_auto_off"]');
      autoOff.addEventListener("change", () => {
        if (!autoOff.value) return;
        send({ action: "set_auto_off", target: "headset", key,
          value: autoOff.value === "never" ? null : Number(autoOff.value) });
      });

      const bands = Array.from(dialog.querySelectorAll("[data-eq-band]"));
      // The whole EQ goes out once a slider is let go: every change is ten
      // reports to the dongle, so not on every step of a drag.
      const gains = () => bands.map((input) => Number(input.value));
      const sendEq = () => send({ action: "set_eq", target: "headset", key, value: gains() });
      bands.forEach((input) => {
        const readout = input.closest(".hid-eq-band").querySelector("[data-eq-readout]");
        input.addEventListener("input", () => {
          input.dataset.dragging = "1";
          readout.textContent = formatGain(Number(input.value));
          // "Custom" the moment a slider leaves the preset, not on release.
          showPreset(dialog, gains(), matchingPreset(gains()));
        });
        input.addEventListener("change", () => {
          input.dataset.dragging = "0";
          sendEq();
        });
        // Right-click resets the band to 0 dB instead of opening the
        // browser context menu, like the volume faders' reset to 100%.
        input.addEventListener("contextmenu", (evt) => {
          evt.preventDefault();
          if (input.matches(":disabled")) return;
          input.value = 0;
          readout.textContent = formatGain(0);
          showPreset(dialog, gains(), matchingPreset(gains()));
          sendEq();
        });
      });

      const select = dialog.querySelector("[data-hid-preset]");
      select.addEventListener("change", () => {
        if (select.value) send({ action: "apply_eq_preset", target: "headset", key, value: select.value });
      });

      const form = dialog.querySelector("[data-hid-preset-form]");
      form.addEventListener("submit", (evt) => {
        evt.preventDefault();
        const input = form.elements.name;
        const name = input.value.trim();
        if (!name) return;
        // The server refuses these too (eq_presets.save), but silently.
        const builtIn = eqPresets.some((p) => p.builtin && p.name.toLowerCase() === name.toLowerCase());
        input.setCustomValidity(builtIn ? "That's a built-in preset. Pick another name." : "");
        if (builtIn) {
          input.reportValidity();
          input.addEventListener("input", () => input.setCustomValidity(""), { once: true });
          return;
        }
        send({ action: "save_eq_preset", value: { name, gains: gains() } });
        form.reset();
      });
      dialog.querySelector("[data-hid-preset-delete]").addEventListener("click", () => {
        if (select.value) send({ action: "delete_eq_preset", value: select.value });
      });
      dialog.querySelector("[data-hid-flat]").addEventListener("click", () => {
        bands.forEach((input) => { input.value = 0; });
        sendEq();
      });
    });
  }

  function applyCard(target, key, directions) {
    const attr = target === "headset" ? "data-headset" : "data-peer";
    const card = document.querySelector(`[${attr}="${cssEscape(key)}"]`);
    if (!card) return;
    for (const [direction, dv] of directions) {
      if (dv) applyDirection(card, direction, dv);
    }
  }

  // Skips writing to any control the user currently has focus on/is
  // dragging - the whole reason this is per-control instead of the old
  // whole-card outerHTML swap, which is what clobbered active drags before.
  function applyDirection(card, direction, dv) {
    const section = card.querySelector(`.strip[data-direction="${direction}"]`);
    if (!section) return;

    // dv.ts, when present, is the client's own Date.now() that caused this
    // broadcast (see app/ws.py's broadcast_control) - never a
    // server-generated one, so this is always comparing this browser tab's
    // own clock against itself. Ignore anything that doesn't yet reflect
    // this tab's own last input for this control (see lastSentTs's
    // docstring for why "last sent", not "last shown", is the bar).
    // Watcher-triggered broadcasts (a physical knob turn) carry no ts at
    // all and always pass through here - nothing to compare against.
    if (dv.ts != null) {
      const target = card.dataset.headset ? "headset" : "peer";
      const key = card.dataset.headset || card.dataset.peer;
      const sentTs = lastSentTs.get(tsKeyFor(target, key, direction));
      if (sentTs != null && dv.ts < sentTs) return;
    }

    const offline = section.querySelector(".offline");
    if (offline) offline.hidden = dv.connected;

    const fader = section.querySelector('[data-action="volume"]');
    if (fader) {
      fader.dataset.disabled = dv.connected ? "0" : "1";
      const dragging = fader.dataset.dragging === "1";
      if (!dragging && dv.volume != null) {
        setFaderValue(fader, dv.volume);
      }
    } else {
      setReadout(section, dv.volume);
    }

    // Icon-only button (see _icons.html) - only title/class communicate
    // muted state now, never textContent, which would blow away the SVG
    // icon markup inside. data-mute-label holds the direction-specific
    // "unmuted" tooltip (e.g. "Mute speakers") set by the template, since
    // that phrasing differs per strip and isn't recoverable once muted.
    const muteBtn = section.querySelector('[data-action="mute"]');
    if (muteBtn) {
      muteBtn.disabled = !dv.connected;
      muteBtn.classList.toggle("active", !!dv.muted);
      muteBtn.title = dv.muted ? "Muted" : (muteBtn.dataset.muteLabel || "Mute");
    }

    const pad = section.querySelector('[data-action="balance-pad"]');
    if (pad && pad.dataset.dragging !== "1" && dv.balance != null) {
      setPadPosition(pad, dv.balance);
    }

    updateCardStatus(card);
    placeDevice(card);
  }

  // Recomputes the card-level status dot from its strips' current
  // .offline visibility - reuses that as the source of truth (rather than
  // tracking connected state separately) so this can't drift from what
  // the strips themselves already show. A disabled headset shows its own
  // "disabled" status instead of "offline", same distinction the server
  // template makes on first render.
  //
  // Peer cards with a Bragi Client tray app (data-client-tracked="true",
  // see _peer_card.html) are the one exception: their dot means "is the
  // tray app's WebSocket connected", not "does a local PipeWire module
  // exist" (which a Roc module can't disprove - it's UDP, see
  // client/README.md) - applyPeerPresence owns that dot exclusively, so
  // this must not overwrite it on every unrelated fader/mute update.
  function updateCardStatus(card) {
    if (card.dataset.clientTracked === "true") return;
    const dot = card.querySelector(".status-dot");
    if (!dot) return;
    if (card.classList.contains("headset-disabled")) {
      dot.className = "status-dot disabled";
      return;
    }
    const strips = card.querySelectorAll(".strip .offline");
    const connectedCount = Array.from(strips).filter((el) => el.hidden).length;
    const status = connectedCount === strips.length ? "online" : connectedCount === 0 ? "offline" : "partial";
    dot.className = `status-dot ${status}`;
  }

  // Presence changes from a peer's own tray app connecting/disconnecting
  // its heartbeat WebSocket (app/main.py's /ws/peer/{name}) - see
  // app/peer_presence.py for why this is more honest than PipeWire node
  // presence for Roc peers.
  function applyPeerPresence(msg) {
    const card = document.querySelector(`[data-peer="${cssEscape(msg.name)}"]`);
    if (!card) return;
    const dot = card.querySelector(".status-dot");
    if (!dot) return;
    dot.className = `status-dot ${msg.connected ? "online" : "offline"}`;
    dot.title = msg.connected ? "Client connected" : "Client not connected to Bragi";
    card.dataset.clientConnected = String(!!msg.connected);
    placeDevice(card);
    if (!msg.connected) {
      const status = card.querySelector(".client-stream-status");
      if (status) status.textContent = "Android service disconnected.";
    }
  }

  function applyPeerStreams(msg) {
    const controls = document.querySelector(`[data-peer="${cssEscape(msg.name)}"] .client-controls`);
    if (!controls) return;
    controls.dataset.sendEnabled = String(msg.send_enabled);
    controls.dataset.receiveEnabled = String(msg.receive_enabled);
    // Listen-only phones have no Send button.
    controls.querySelector('[data-stream="send"]')?.classList.toggle("active", msg.send_enabled);
    controls.querySelector('[data-stream="receive"]')?.classList.toggle("active", msg.receive_enabled);
  }

  function applyPeerStatus(msg) {
    const status = document.querySelector(`[data-peer="${cssEscape(msg.name)}"] .client-stream-status`);
    if (!status) return;
    // Delays are measured on the phone only; network and Pi time come on top.
    const delay = (active, ms) => (active && ms != null ? ` ${ms} ms` : "");
    // A wrong address means silence, so it outranks everything else.
    status.textContent = msg.address_warning || msg.error ||
      `${msg.send_active ? "Sending" : "Sender paused"}${delay(msg.send_active, msg.send_delay_ms)} · ` +
      `${msg.receive_active ? "Listening" : "Receiver paused"}${delay(msg.receive_active, msg.listen_delay_ms)}`;
    // The desktop strip truncates this line, so the tooltip carries all of it.
    status.title = `${status.textContent} · Android · ${status.dataset.ip}`;
  }

  // --- vertical fader: a custom pointer-driven control, not a native
  // <input type=range> rotated via writing-mode - that combination has a
  // known cross-browser quirk where drag/click position gets mapped using
  // the pre-rotation bounding box, which is what caused the fader to
  // visibly jump on click/drag. This sidesteps that entirely, the same way
  // the balance pad already did (which never had the jumping problem).

  function setFaderValue(fader, value, opts = {}) {
    const max = parseFloat(fader.dataset.max || "1.5");
    value = Math.max(0, Math.min(max, value));
    fader.dataset.value = value;
    // style.css turns --frac into a vertical or horizontal position,
    // depending on the layout (desktop strips or phone rows).
    fader.style.setProperty("--frac", value / max);
    if (!opts.silent) setReadout(fader.closest(".strip"), value);
  }

  function wireFaders(root) {
    root.querySelectorAll('.fader[data-action="volume"]').forEach((fader) => {
      const max = parseFloat(fader.dataset.max || "1.5");
      const sendThrottled = throttle((value) => {
        const ts = nextTs();
        lastSentTs.set(tsKeyFor(fader.dataset.target, fader.dataset.key, fader.dataset.direction), ts);
        send({
          action: "set_volume",
          target: fader.dataset.target,
          key: fader.dataset.key,
          direction: fader.dataset.direction,
          value,
          ts,
        });
      }, THROTTLE_MS);

      function updateFromPointer(evt) {
        if (fader.dataset.disabled === "1") return;
        const rect = fader.getBoundingClientRect();
        // Phone rows lay faders out horizontally (see style.css).
        const raw = rect.width > rect.height
          ? (evt.clientX - rect.left) / rect.width
          : (rect.bottom - evt.clientY) / rect.height;
        const frac = Math.max(0, Math.min(1, raw));
        const value = frac * max;
        setFaderValue(fader, value);
        sendThrottled(value);
      }

      fader.addEventListener("pointerdown", (evt) => {
        if (fader.dataset.disabled === "1") return;
        fader.dataset.dragging = "1";
        fader.setPointerCapture(evt.pointerId);
        updateFromPointer(evt);
      });
      fader.addEventListener("pointermove", (evt) => {
        if (fader.dataset.dragging === "1") updateFromPointer(evt);
      });
      const release = () => {
        fader.dataset.dragging = "0";
      };
      fader.addEventListener("pointerup", release);
      fader.addEventListener("pointercancel", release);

      // Right-click resets to 100% (unity gain) instead of opening the
      // browser context menu.
      fader.addEventListener("contextmenu", (evt) => {
        evt.preventDefault();
        if (fader.dataset.disabled === "1") return;
        setFaderValue(fader, 1.0);
        sendThrottled(1.0);
      });

      // Paint the server-rendered initial value (SSR already put it in
      // data-value) now that the element has real layout to measure.
      setFaderValue(fader, parseFloat(fader.dataset.value || "1"), { silent: true });
      setReadout(fader.closest(".strip"), parseFloat(fader.dataset.value || "1"));
    });
  }

  // --- balance pad: same pointer-driven pattern as the fader above. Present
  // on both peer strips (outgoing and incoming are both software Roc/VBAN
  // stream nodes) - headset strips have no pad at all, since headset
  // balance doesn't hold on real ALSA hardware (WirePlumber overrides it).

  function setPadPosition(pad, balance) {
    const dot = pad.querySelector(".pad-dot");
    if (!dot) return;
    const x = ((balance + 1) / 2) * 100;
    dot.style.left = `${x}%`;
  }

  function wirePads(root) {
    root.querySelectorAll('.pad[data-action="balance-pad"]').forEach((pad) => {
      const sendBalance = throttle((balance) => {
        const ts = nextTs();
        lastSentTs.set(tsKeyFor(pad.dataset.target, pad.dataset.key, pad.dataset.direction), ts);
        send({
          action: "set_balance",
          target: pad.dataset.target,
          key: pad.dataset.key,
          direction: pad.dataset.direction,
          value: balance,
          ts,
        });
      }, THROTTLE_MS);

      function updateFromPointer(evt) {
        const rect = pad.getBoundingClientRect();
        const x = Math.max(0, Math.min(1, (evt.clientX - rect.left) / rect.width));
        const balance = x * 2 - 1;
        setPadPosition(pad, balance);
        sendBalance(balance);
      }

      pad.addEventListener("pointerdown", (evt) => {
        pad.dataset.dragging = "1";
        pad.setPointerCapture(evt.pointerId);
        updateFromPointer(evt);
      });
      pad.addEventListener("pointermove", (evt) => {
        if (pad.dataset.dragging === "1") updateFromPointer(evt);
      });
      const release = () => {
        pad.dataset.dragging = "0";
      };
      pad.addEventListener("pointerup", release);
      pad.addEventListener("pointercancel", release);

      // Right-click resets to centered instead of opening the browser
      // context menu.
      pad.addEventListener("contextmenu", (evt) => {
        evt.preventDefault();
        setPadPosition(pad, 0);
        sendBalance(0);
      });
    });
  }

  function throttle(fn, ms) {
    let last = 0;
    let timer = null;
    let pendingArgs = null;
    return (...args) => {
      const now = Date.now();
      if (now - last >= ms) {
        last = now;
        fn(...args);
      } else {
        pendingArgs = args;
        if (!timer) {
          timer = setTimeout(() => {
            timer = null;
            last = Date.now();
            fn(...pendingArgs);
          }, ms - (now - last));
        }
      }
    };
  }

  // Everything inside one card: run for the whole page on load, and again
  // for whatever htmx swaps in (see the htmx:afterSwap listener below) -
  // freshly rendered cards carry no listeners of their own.
  function wireControls(root) {
    wireFaders(root);
    wirePads(root);
    wireHeadsetControls(root);

    root.querySelectorAll('button[data-action="toggle-stream"]').forEach((el) => {
      el.addEventListener("click", () => {
        const controls = el.closest(".client-controls");
        const sendEnabled = controls.dataset.sendEnabled === "true";
        const receiveEnabled = controls.dataset.receiveEnabled === "true";
        send({ action: "set_peer_streams", target: "peer", key: el.dataset.key,
          send_enabled: el.dataset.stream === "send" ? !sendEnabled : sendEnabled,
          receive_enabled: el.dataset.stream === "receive" ? !receiveEnabled : receiveEnabled });
      });
    });

    root.querySelectorAll('button[data-action="mute"]').forEach((el) => {
      el.addEventListener("click", () => {
        send({
          action: "toggle_mute",
          target: el.dataset.target,
          key: el.dataset.key,
          direction: el.dataset.direction,
        });
      });
    });

    root.querySelectorAll('button[data-action="toggle_enabled"]').forEach((el) => {
      el.addEventListener("click", () => {
        send({
          action: "toggle_enabled",
          target: el.dataset.target,
          key: el.dataset.key,
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    wireControls(document);
    wireUnitToggle();
    wireSettingsDialog();
    wireAddPeerDialog();
    wireBluetoothDialog();
    wireReorder();
    scheduleRowCount();
    window.addEventListener("resize", scheduleRowCount);
    connect();
  });

  // Adding or removing a peer swaps the whole peers list (the one place
  // htmx still owns). Every peer card in it is new: none of them has a
  // listener until it's wired, so without this every peer's faders and
  // mute buttons went dead after any add or remove, until a reload. The
  // swap also detaches every cached .level-fill.
  document.addEventListener("htmx:afterSwap", (evt) => {
    levelFills.clear();
    wireControls(evt.detail.target);
    scheduleRowCount();
  });
})();
