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

  function connect() {
    setStatus("connecting");
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
      else if (msg.type === "peer_presence") applyPeerPresence(msg);
      else if (msg.type === "levels") applyLevels(msg);
      else if (msg.type === "viz_settings") applyVizSettings(msg);
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
    if (displayMode === "db") {
      if (volume <= 0.0001) return "-\u221e dB";
      return `${(20 * Math.log10(volume)).toFixed(1)} dB`;
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
      if (fill) fill.style.height = `${Math.max(0, Math.min(1, level.value)) * 100}%`;
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
    const toggleBtn = card.querySelector('[data-action="toggle_enabled"]');
    if (toggleBtn) toggleBtn.title = msg.enabled ? "Disable headset" : "Enable headset";
    if (msg.playback) applyDirection(card, "playback", msg.playback);
    if (msg.capture) applyDirection(card, "capture", msg.capture);
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
    dot.title = msg.connected ? "Tray app connected" : "Tray app not connected to sagepi";
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
    const frac = value / max;
    const fill = fader.querySelector(".fader-fill");
    const thumb = fader.querySelector(".fader-thumb");
    if (fill) fill.style.height = `${frac * 100}%`;
    if (thumb) thumb.style.bottom = `${frac * 100}%`;
    if (!opts.silent) setReadout(fader.closest(".strip"), value);
  }

  function wireFaders() {
    document.querySelectorAll('.fader[data-action="volume"]').forEach((fader) => {
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
        const frac = Math.max(0, Math.min(1, (rect.bottom - evt.clientY) / rect.height));
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

  function wirePads() {
    document.querySelectorAll('.pad[data-action="balance-pad"]').forEach((pad) => {
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

  function wireControls() {
    wireFaders();
    wirePads();
    wireUnitToggle();
    wireSettingsDialog();

    document.querySelectorAll('button[data-action="mute"]').forEach((el) => {
      el.addEventListener("click", () => {
        send({
          action: "toggle_mute",
          target: el.dataset.target,
          key: el.dataset.key,
          direction: el.dataset.direction,
        });
      });
    });

    document.querySelectorAll('button[data-action="toggle_enabled"]').forEach((el) => {
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
    wireControls();
    connect();
  });

  // Adding or removing a peer swaps the whole peers list (the one place
  // htmx still owns), which detaches every cached .level-fill in it.
  document.addEventListener("htmx:afterSwap", () => levelFills.clear());
})();
