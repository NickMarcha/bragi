// KVM setup (app/kvm_config.py): the computers that share the desk's
// keyboard and mouse, where they sit, the hotkeys and the input options.
// Loads /kvm/config when the dialog opens; each section saves on its own.
(() => {
  const dialog = document.getElementById("kvm-setup-dialog");
  if (!dialog) return;

  const GRID_COLS = 5;
  const GRID_ROWS = 3;
  let config = null;
  let selected = null; // computer picked in the layout grid, waiting for a cell

  const $ = (sel) => dialog.querySelector(sel);
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== false && v !== null && v !== undefined) node.setAttribute(k, v === true ? "" : v);
    }
    for (const child of children.flat()) node.append(child instanceof Node ? child : document.createTextNode(child));
    return node;
  };

  async function call(method, url, body) {
    const response = await fetch(url, {
      method, credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await response.json(); } catch (_) { /* empty */ }
    if (!response.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `HTTP ${response.status}`);
    return data;
  }

  function say(section, text, isError = false) {
    const node = $(`[data-msg="${section}"]`);
    node.textContent = text || "";
    node.className = isError ? "form-error" : "form-note";
    node.hidden = !text;
  }

  async function save(section, url, body, button) {
    say(section, "Saving…");
    button.disabled = true;
    try {
      const result = await call("POST", url, body);
      config = result.config;
      render();
      say(section, result.note);
    } catch (err) {
      say(section, err.message, true);
    } finally {
      button.disabled = false;
    }
  }

  const names = () => config.computers.map((c) => c.name);

  // --- computers ---------------------------------------------------------------

  function computerRow(c = { name: "", aliases: [] }) {
    return el("li", { class: "kvm-row" },
      el("input", { type: "text", class: "kvm-name", value: c.name, placeholder: "name", "aria-label": "Computer name", maxlength: 63 }),
      el("input", { type: "text", class: "kvm-aliases", value: c.aliases.join(", "),
                    placeholder: "other names it connects as (optional)", "aria-label": "Aliases" }),
      el("button", { type: "button", title: "Remove", onclick: (e) => e.target.closest("li").remove() }, "Remove"));
  }

  function renderComputers() {
    const list = $("#kvm-computers");
    list.replaceChildren(...config.computers.map(computerRow));
  }

  // --- layout ------------------------------------------------------------------

  function renderLayout() {
    const grid = $("#kvm-grid");
    grid.style.setProperty("--cols", GRID_COLS);
    const at = {};
    for (const [name, [c, r]] of Object.entries(config.cells)) at[`${c},${r}`] = name;
    const cells = [];
    for (let r = 0; r < GRID_ROWS; r++) {
      for (let c = 0; c < GRID_COLS; c++) {
        const name = at[`${c},${r}`];
        cells.push(el("button", {
          type: "button",
          class: "kvm-cell" + (name ? " taken" : "") + (name && name === selected ? " selected" : ""),
          "aria-label": name ? `${name}, click to move it` : `Empty place ${c + 1},${r + 1}`,
          onclick: () => place(c, r, name),
        }, name || ""));
      }
    }
    grid.replaceChildren(...cells);
    const unplaced = names().filter((n) => !(n in config.cells));
    $("#kvm-unplaced").replaceChildren(...unplaced.map((n) => el("button", {
      type: "button", class: "kvm-chip" + (n === selected ? " selected" : ""), onclick: () => { selected = n; renderLayout(); },
    }, n)));
    $("#kvm-unplaced-note").hidden = !unplaced.length;
  }

  function place(c, r, name) {
    if (!selected) {
      if (name) { selected = name; renderLayout(); }
      return;
    }
    if (name && name !== selected) {
      // Swap: the one there takes the selected one's place (or leaves the grid).
      const from = config.cells[selected];
      if (from) config.cells[name] = from; else delete config.cells[name];
    }
    config.cells[selected] = [c, r];
    selected = null;
    renderLayout();
  }

  // --- hotkeys -----------------------------------------------------------------

  // keystroke(KEYS) = ACTION, parsed into a form when it's one of these.
  const ACTIONS = [
    ["switch", "Switch to computer"],
    ["next", "Next computer"],
    ["send", "Send keys to computers"],
    ["lock", "Lock the cursor to the computer (toggle)"],
    ["raw", "Other (as written)"],
  ];

  function parseHotkey(line) {
    const m = line.match(/^keystroke\(([^)]*)\)\s*=\s*(.+)$/);
    if (!m) return { kind: "raw", raw: line };
    const keys = m[1].trim();
    const action = m[2].trim();
    let a;
    if ((a = action.match(/^switchToScreen\(([^)]+)\)$/))) return { kind: "switch", keys, target: a[1].trim() };
    if (action === "switchToNextScreen") return { kind: "next", keys };
    if (action === "lockCursorToScreen(toggle)") return { kind: "lock", keys };
    if ((a = action.match(/^keystroke\(([^,()]+),([A-Za-z0-9._:-]+)\)$/))) {
      return { kind: "send", keys, send: a[1].trim(), targets: a[2].split(":") };
    }
    return { kind: "raw", raw: line };
  }

  function hotkeyLine(row) {
    const kind = row.querySelector(".kvm-action").value;
    const keys = row.querySelector(".kvm-keys").value.trim();
    if (kind === "raw") return row.querySelector(".kvm-raw").value.trim();
    if (!keys) return "";
    if (kind === "switch") return `keystroke(${keys}) = switchToScreen(${row.querySelector(".kvm-target").value})`;
    if (kind === "next") return `keystroke(${keys}) = switchToNextScreen`;
    if (kind === "lock") return `keystroke(${keys}) = lockCursorToScreen(toggle)`;
    const send = row.querySelector(".kvm-send").value.trim();
    const targets = [...row.querySelectorAll(".kvm-targets input:checked")].map((i) => i.value);
    return send && targets.length ? `keystroke(${keys}) = keystroke(${send},${targets.join(":")})` : "";
  }

  function hotkeyRow(line = "keystroke() = switchToNextScreen") {
    const h = parseHotkey(line);
    const row = el("li", { class: "kvm-hotkey" });
    const action = el("select", { class: "kvm-action", "aria-label": "Action" },
      ACTIONS.map(([v, label]) => el("option", { value: v, selected: v === h.kind }, label)));
    const keys = el("input", { type: "text", class: "kvm-keys", value: h.keys || "", placeholder: "Control+Alt+F9",
                               "aria-label": "Keys", title: "Modifiers and a key joined with +, e.g. Control+Alt+F12" });
    const target = el("select", { class: "kvm-target", "aria-label": "Computer" },
      names().map((n) => el("option", { value: n, selected: n === h.target }, n)));
    const send = el("input", { type: "text", class: "kvm-send", value: h.send || "", placeholder: "Control+Shift+Alt+M",
                               "aria-label": "Keys to send" });
    const targets = el("span", { class: "kvm-targets" }, names().map((n) => el("label", {},
      el("input", { type: "checkbox", value: n, checked: (h.targets || []).includes(n) }), n)));
    const raw = el("input", { type: "text", class: "kvm-raw", value: h.raw || "", "aria-label": "Hotkey line",
                              placeholder: "keystroke(...) = ..." });
    const remove = el("button", { type: "button", onclick: () => row.remove() }, "Remove");
    const show = () => {
      const k = action.value;
      keys.hidden = k === "raw";
      target.hidden = k !== "switch";
      send.hidden = targets.hidden = k !== "send";
      raw.hidden = k !== "raw";
      if (k === "raw" && !raw.value) raw.value = line;
    };
    action.addEventListener("change", show);
    row.append(keys, action, target, send, targets, raw, remove);
    show();
    return row;
  }

  function renderHotkeys() {
    $("#kvm-hotkeys").replaceChildren(...config.hotkeys.map((line) => hotkeyRow(line)));
  }

  // --- mouse buttons (app/hidpp.py) --------------------------------------------------

  // One select for every action: "none", "kvm_next", "kvm_keys" or "switch:<name>".
  function mouseChoice(action) {
    if (action.type === "kvm_switch") return `switch:${action.computer}`;
    return action.type;
  }

  function mouseRow(device, control, computers) {
    const a = control.action;
    const choice = el("select", { "aria-label": `${control.label} action` },
      el("option", { value: "none", selected: a.type === "none" }, "Nothing (a normal button)"),
      el("option", { value: "kvm_next", selected: a.type === "kvm_next" }, "Next computer"),
      computers.map((n) => el("option", { value: `switch:${n}`, selected: mouseChoice(a) === `switch:${n}` }, `Switch to ${n}`)),
      el("option", { value: "kvm_keys", selected: a.type === "kvm_keys" }, "Press keys on computers"));
    const keys = el("input", { type: "text", class: "kvm-send", value: a.keys || "", placeholder: "Control+Shift+Alt+M",
                               "aria-label": "Keys to press" });
    const targets = el("span", { class: "kvm-targets" }, computers.map((n) => el("label", {},
      el("input", { type: "checkbox", value: n, checked: (a.computers || []).includes(n) }), n)));
    const note = el("span", { class: "form-note" }, control.result
      ? `Last: ${control.result.text}` : "");
    const show = () => { keys.hidden = targets.hidden = choice.value !== "kvm_keys"; };
    const save = async () => {
      show();
      const v = choice.value;
      let action = { type: v };
      if (v.startsWith("switch:")) action = { type: "kvm_switch", computer: v.slice(7) };
      if (v === "kvm_keys") {
        if (!keys.value.trim()) return;
        action = { type: v, keys: keys.value.trim(),
                   computers: [...targets.querySelectorAll("input:checked")].map((i) => i.value) };
      }
      say("mouse", "Saving…");
      try {
        renderMouse(await call("POST", "/mouse-buttons", { device: device.name, control: control.id, action }));
        say("mouse", "Saved.");
      } catch (err) {
        say("mouse", err.message, true);
      }
    };
    choice.addEventListener("change", save);
    keys.addEventListener("change", save);
    targets.addEventListener("change", save);
    const row = el("li", { class: "kvm-hotkey" }, el("span", { class: "kvm-keys" }, control.label), choice, keys, targets, note);
    show();
    return row;
  }

  function renderMouse(view) {
    const list = $("#kvm-mouse");
    const rows = [];
    for (const device of view.devices) {
      const battery = device.battery ? `, battery ${device.battery.percent}% ${device.battery.state}` : "";
      rows.push(el("li", {}, el("strong", {}, device.name || `Device in slot ${device.slot}`), battery));
      if (!device.controls.length) rows.push(el("li", { class: "form-note" }, "No extra buttons."));
      for (const control of device.controls) rows.push(mouseRow(device, control, view.computers));
    }
    if (!rows.length) rows.push(el("li", { class: "form-note" }, "No Logitech receiver with a paired device found. A mouse that's asleep shows up once it's moved."));
    list.replaceChildren(...rows);
    const fork = view.fork || {};
    $("#kvm-fork-hidpp").hidden = !(fork.listening || (fork.bindings || []).length);
  }

  async function loadMouse() {
    try {
      renderMouse(await call("GET", "/mouse-buttons"));
    } catch (err) {
      say("mouse", err.message, true);
    }
  }

  $("#kvm-hand-over").addEventListener("click", async (e) => {
    if (!confirm("This turns the KVM server's own mouse-button handling off and restarts it; every computer reconnects. Go on?")) return;
    e.target.disabled = true;
    say("mouse", "Restarting the KVM…");
    try {
      const view = await call("POST", "/mouse-buttons/hand-over");
      renderMouse(view);
      say("mouse", view.note);
    } catch (err) {
      say("mouse", err.message, true);
    } finally {
      e.target.disabled = false;
    }
  });

  // --- keys and buttons (app/key_bindings.py) ------------------------------------------

  let bindings = { bindings: [], last_input: null };
  let capture = null; // {phase: "waiting", baseline, started, refreshed} or {phase: "captured", device, code, input}
  let captureTimer = null;

  function renderBindings() {
    const list = $("#kvm-bindings");
    const rows = bindings.bindings.map((b, i) => el("li", { class: "kvm-row" },
      el("span", {}, el("strong", {}, b.input), ` on ${b.device || "any device"} sends `, el("strong", {}, b.label || b.sends)),
      el("button", { type: "button", onclick: () => saveBindings(bindings.bindings.filter((_, j) => j !== i)) }, "Remove")));
    list.replaceChildren(...(rows.length ? rows : [el("li", { class: "form-note" }, "No bindings.")]));
    renderEditor();
  }

  async function saveBindings(list) {
    say("bindings", "Saving…");
    try {
      const out = list.map((b) => ({ device: b.device, code: b.code, send: b.send, keys: b.keys, label: b.label }));
      bindings = await call("POST", "/kvm/bindings", { bindings: out });
      say("bindings", bindings.note);
      renderBindings();
      return true;
    } catch (err) {
      say("bindings", err.message, true);
      return false;
    }
  }

  async function stopCapture(keep) {
    clearInterval(captureTimer);
    captureTimer = null;
    if (!keep) capture = null;
    try { await call("POST", "/kvm/bindings/identify", { on: false }); } catch (_) { /* best effort */ }
    renderEditor();
  }

  async function startCapture() {
    try {
      bindings = await call("POST", "/kvm/bindings/identify", { on: true });
    } catch (err) {
      say("bindings", err.message, true);
      return;
    }
    capture = { phase: "waiting", baseline: bindings.last_input && bindings.last_input.at, started: Date.now(), refreshed: Date.now() };
    renderEditor();
    clearInterval(captureTimer);
    captureTimer = setInterval(async () => {
      if (Date.now() - capture.started > 120000) return stopCapture(false);
      if (Date.now() - capture.refreshed > 20000) {
        capture.refreshed = Date.now();
        call("POST", "/kvm/bindings/identify", { on: true }).catch(() => {});
      }
      let now;
      try { now = await call("GET", "/kvm/bindings"); } catch (_) { return; }
      const last = now.last_input;
      if (capture && capture.phase === "waiting" && last && last.at && last.at !== capture.baseline) {
        capture = { phase: "captured", device: last.device, code: last.code, input: last.input };
        stopCapture(true);
      }
    }, 700);
  }

  function renderEditor() {
    const box = $("#kvm-binding-editor");
    $("#kvm-add-binding").disabled = capture !== null;
    if (!capture) { box.replaceChildren(); return; }
    if (capture.phase === "waiting") {
      box.replaceChildren(el("p", { class: "form-note" }, "Press the key or button to remap, on the box's keyboard or mouse. ",
        el("button", { type: "button", onclick: () => stopCapture(false) }, "Cancel")));
      return;
    }
    const send = el("input", { type: "text", class: "kvm-send", placeholder: "Control+Super+Right", "aria-label": "Send instead" });
    const pick = (label, keys) => el("button", { type: "button", onclick: () => { send.value = keys; } }, label);
    const hotkeyPicks = (config ? config.hotkeys : []).map(parseHotkey).filter((h) => h.keys && h.kind !== "raw")
      .map((h) => pick(`${h.keys} (${h.kind === "switch" ? `switch to ${h.target}` : h.kind === "next" ? "next computer" : h.kind === "lock" ? "lock cursor" : "send keys"})`, h.keys));
    const only = el("input", { type: "checkbox", checked: true });
    box.replaceChildren(
      el("p", {}, "Pressed ", el("strong", {}, capture.input), ` on ${capture.device || "an unnamed device"}. `,
        el("button", { type: "button", onclick: startCapture }, "Press a different one")),
      el("div", { class: "kvm-row" }, el("span", {}, "Send instead"), send),
      el("div", { class: "kvm-chips" }, pick("Next virtual desktop", "Control+Super+Right"),
        pick("Previous virtual desktop", "Control+Super+Left"), ...hotkeyPicks),
      el("label", {}, only, " Only on this device"),
      el("div", { class: "dialog-actions" },
        el("button", { type: "button", class: "primary", onclick: async () => {
          if (!send.value.trim()) { say("bindings", "Type what to send, or pick one.", true); return; }
          const device = only.checked ? capture.device : "";
          const kept = bindings.bindings.filter((b) => !(b.device === device && b.code === capture.code));
          if (await saveBindings([...kept, { device, code: capture.code, keys: send.value.trim() }])) {
            capture = null;
            renderEditor();
          }
        } }, "Save"),
        el("button", { type: "button", onclick: () => stopCapture(false) }, "Cancel")));
  }

  async function loadBindings() {
    try {
      bindings = await call("GET", "/kvm/bindings");
      renderBindings();
    } catch (err) {
      say("bindings", err.message, true);
    }
  }

  $("#kvm-add-binding").addEventListener("click", startCapture);
  dialog.addEventListener("close", () => { if (capture) stopCapture(false); });

  // --- input -------------------------------------------------------------------

  function renderInput() {
    for (const input of dialog.querySelectorAll("[data-input]")) input.value = config.input[input.dataset.input] || "";
  }

  function render() {
    $("#kvm-host").textContent = config.host;
    renderComputers();
    renderLayout();
    renderHotkeys();
    renderInput();
  }

  // --- wiring --------------------------------------------------------------------

  $("#kvm-add-computer").addEventListener("click", () => $("#kvm-computers").append(computerRow()));
  $("#kvm-save-computers").addEventListener("click", (e) => {
    const computers = [...dialog.querySelectorAll("#kvm-computers li")].map((li) => ({
      name: li.querySelector(".kvm-name").value.trim(),
      aliases: li.querySelector(".kvm-aliases").value.split(",").map((a) => a.trim()).filter(Boolean),
    })).filter((c) => c.name);
    if (!confirm("Saving computers restarts the KVM; every computer reconnects. Save?")) return;
    save("computers", "/kvm/config/computers", { computers }, e.target);
  });
  $("#kvm-unplace").addEventListener("click", () => {
    if (selected) { delete config.cells[selected]; selected = null; renderLayout(); }
  });
  $("#kvm-identify").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const { shown, missing } = await call("POST", "/kvm/identify");
      const parts = [];
      const reached = Object.values(shown).filter(Boolean);
      if (reached.length) parts.push(`Showing the name on ${reached.join(", ")}.`);
      const strangers = Object.entries(shown).filter(([, c]) => !c).map(([peer]) => peer);
      if (strangers.length) parts.push(`${strangers.join(", ")} has the Bragi app but isn't one of these computers (add it, or its name as an alias).`);
      if (missing.length) parts.push(`No Bragi app connected on ${missing.join(", ")}, so nothing shows there.`);
      say("layout", parts.join(" ") || "No computers to identify.");
    } catch (err) {
      say("layout", err.message, true);
    } finally {
      setTimeout(() => { e.target.disabled = false; }, 4000);
    }
  });
  $("#kvm-add-hotkey").addEventListener("click", () => $("#kvm-hotkeys").append(hotkeyRow()));
  const saveLayout = (e) => {
    const hotkeys = [...dialog.querySelectorAll("#kvm-hotkeys li")].map(hotkeyLine).filter(Boolean);
    save(e.target.dataset.section, "/kvm/config/layout", { cells: config.cells, hotkeys }, e.target);
  };
  $("#kvm-save-layout").addEventListener("click", saveLayout);
  $("#kvm-save-hotkeys").addEventListener("click", saveLayout);
  $("#kvm-save-input").addEventListener("click", (e) => {
    const input = {};
    for (const field of dialog.querySelectorAll("[data-input]")) input[field.dataset.input] = field.value.trim();
    if (!confirm("Saving input options restarts the KVM; every computer reconnects. Save?")) return;
    save("input", "/kvm/config/input", { input }, e.target);
  });

  async function open() {
    for (const s of ["load", "computers", "layout", "hotkeys", "mouse", "bindings", "input"]) say(s, "");
    dialog.showModal();
    try {
      config = await call("GET", "/kvm/config");
      selected = null;
      render();
      loadMouse();
      loadBindings();
    } catch (err) {
      say("load", err.message, true);
    }
  }

  // Delegated: the KVM bar's button is re-rendered by htmx.
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-open-kvm-setup]")) open();
  });
  $("#kvm-setup-close").addEventListener("click", () => dialog.close());
})();
