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

  // --- monitors (app/monitors.py) ------------------------------------------------

  function monitorLine(m) {
    const size = m.width_mm && m.height_mm
      ? `, ${(Math.hypot(m.width_mm, m.height_mm) / 25.4).toFixed(0)}″ (${Math.round(m.width_mm / 10)}×${Math.round(m.height_mm / 10)} cm)`
      : "";
    const turned = m.rotation ? `, turned ${m.rotation}°` : "";
    const mirrored = (m.mirrors || []).length ? `, mirrored on ${m.mirrors.map((r) => r.name).join(" and ")}` : "";
    return `${m.name}: ${m.pixel_width}×${m.pixel_height} at ${Math.round(m.scale * 100)}%${turned}${size}${m.primary ? ", main" : ""}${mirrored}`;
  }

  function renderMonitors(view) {
    const rows = view.computers.map((c) => {
      const state = !c.peer ? "no Bragi tray app has reported yet"
        : c.online ? `${c.monitors.length} monitor${c.monitors.length === 1 ? "" : "s"}`
        : `offline; last seen ${new Date(c.seen * 1000).toLocaleString()}`;
      return el("li", {},
        el("strong", {}, c.computer), " ", el("span", { class: "form-note" }, state),
        c.monitors.length ? el("ul", { class: "kvm-monitor-list" }, ...c.monitors.map((m) => el("li", {}, monitorLine(m)))) : "");
    });
    $("#kvm-monitors").replaceChildren(rows.length ? el("ul", { class: "kvm-monitor-computers" }, ...rows)
      : el("p", { class: "form-note" }, "No computers yet."));
  }

  async function loadMonitors() {
    try {
      monitorView = await call("GET", "/kvm/monitors");
      renderMonitors(monitorView);
      if (!config) return;
      syncGroups();
      view = null;
      changedLayout();
    } catch (err) {
      $("#kvm-monitors").replaceChildren(el("p", { class: "form-error" }, err.message));
    }
  }

  document.addEventListener("bragi:monitors", () => { if (dialog.open) loadMonitors(); });
  // The KVM switched layouts by itself because the setup changed (app/kvm_follow.py).
  document.addEventListener("bragi:kvm-layout", async (e) => {
    if (!dialog.open) return;
    try {
      config = await call("GET", "/kvm/config");
      render();
      say("layout", e.detail.note);
    } catch (_) { /* the next open shows it */ }
  });

  // --- monitor layout (app/kvm_layout.py) ------------------------------------------
  //
  // Each computer is a group: its monitors in its own desktop coordinates,
  // drawn at (x, y) on a shared canvas at `scale` canvas units per desktop
  // unit. The server works out where groups touch (/kvm/config/layout/preview).

  const MM = 96 / 25.4;           // canvas units per millimetre for "real size"
  const TILE = { width: 1920, height: 1080 };
  const SNAP_PX = 12;             // snap distance on screen
  let mode = "grid";
  let groups = {};                // name -> {x, y, scale, size, monitors, tile}
  let monitorView = null;
  let changed = [];               // computers whose monitors differ from the saved layout
  let analysis = null;            // the last preview
  let view = null;                // {x, y, z}: canvas point at the editor's top left, and zoom

  const SIZE_LABELS = { os: "As the computer shows it", physical: "Real size", manual: "Custom" };

  const sameMonitors = (a, b) => JSON.stringify(a.map(({ id, x, y, width, height }) => [id, x, y, width, height]))
    === JSON.stringify(b.map(({ id, x, y, width, height }) => [id, x, y, width, height]));

  function bounds(g) {
    const ms = g.monitors;
    const x0 = Math.min(...ms.map((m) => m.x)), y0 = Math.min(...ms.map((m) => m.y));
    const x1 = Math.max(...ms.map((m) => m.x + m.width)), y1 = Math.max(...ms.map((m) => m.y + m.height));
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }

  // The group's monitors and outline on the canvas: [left, top, right, bottom].
  function rects(g) {
    const b = bounds(g);
    return g.monitors.map((m) => {
      const l = g.x + (m.x - b.x) * g.scale, t = g.y + (m.y - b.y) * g.scale;
      return [l, t, l + m.width * g.scale, t + m.height * g.scale];
    });
  }
  function box(g) {
    const b = bounds(g);
    return [g.x, g.y, g.x + b.w * g.scale, g.y + b.h * g.scale];
  }

  // Real size: the monitors' millimetres per desktop unit, averaged.
  function physicalScale(g) {
    const per = g.monitors.filter((m) => m.width_mm).map((m) => m.width_mm / m.width);
    return per.length ? (per.reduce((a, b) => a + b, 0) / per.length) * MM : null;
  }

  function sizeScale(g) {
    if (g.size === "os") return 1;
    if (g.size === "physical") return physicalScale(g) || 1;
    return g.scale;
  }

  function reported(name) {
    const c = monitorView && monitorView.computers.find((x) => x.computer === name);
    return c && c.monitors.length ? c.monitors.map((m) => ({
      id: m.id, name: m.name, x: m.x, y: m.y, width: m.width, height: m.height,
      width_mm: m.width_mm, pixel_width: m.pixel_width, pixel_height: m.pixel_height,
      mirrors: (m.mirrors || []).map((r) => r.name),
    })) : null;
  }

  // Groups from the saved layout, with each computer's monitors as its tray
  // app reports them now. New computers are placed to the right, apart.
  function initGroups() {
    mode = (config.layout && config.layout.mode) || "grid";
    groups = JSON.parse(JSON.stringify((config.layout && config.layout.groups) || {}));
    syncGroups();
    if (!Object.keys((config.layout && config.layout.groups) || {}).length) arrangeLikeGrid();
  }

  function syncGroups() {
    if (!config) return;
    changed = [];
    const fresh = [];
    for (const name of names()) {
      const now = reported(name);
      let g = groups[name];
      if (!g) {
        g = groups[name] = { x: 0, y: 0, scale: 1, size: now ? "os" : "manual", monitors: [] };
        fresh.push(name);
      }
      if (now) {
        if (g.monitors.length && !g.tile && !sameMonitors(g.monitors, now)) changed.push(name);
        if (g.tile) g.size = "os";
        g.monitors = now;
        g.tile = false;
      } else if (!g.monitors.length) {
        g.monitors = [{ id: "tile", name: "", x: 0, y: 0, ...TILE }];
        g.tile = true;
        g.size = "manual";
        g.scale = 1;
      }
      g.scale = sizeScale(g);
    }
    for (const name of Object.keys(groups)) if (!names().includes(name)) delete groups[name];
    // Newcomers go to the right of everything, with a gap, so nothing links by accident.
    for (const name of fresh) {
      const others = Object.entries(groups).filter(([n]) => n !== name && !fresh.slice(fresh.indexOf(name)).includes(n));
      const right = others.length ? Math.max(...others.map(([, g]) => box(g)[2])) : 0;
      groups[name].x = right + (others.length ? 400 : 0);
      groups[name].y = 0;
    }
  }

  // Rows and columns from the simple grid; each row's computers touch side
  // by side, rows touch top to bottom.
  function arrangeLikeGrid() {
    const placed = Object.entries(config.cells).filter(([n]) => groups[n]).sort(([, a], [, b]) => a[1] - b[1] || a[0] - b[0]);
    const rest = names().filter((n) => !(n in config.cells) && groups[n]);
    const rows = new Map();
    for (const [n, [, r]] of placed) rows.set(r, [...(rows.get(r) || []), n]);
    if (rest.length) rows.set(Infinity, rest);
    let y = 0;
    for (const row of rows.values()) {
      let x = 0, height = 0;
      for (const n of row) {
        const g = groups[n];
        const [, , r, b] = box({ ...g, x: 0, y: 0 });
        g.x = x; g.y = y;
        x += r; height = Math.max(height, b);
      }
      y += height;
    }
  }

  // --- drawing

  function fit() {
    const area = $("#kvm-canvas");
    const all = Object.values(groups).map(box);
    if (!all.length || !area.clientWidth) return null;
    const l = Math.min(...all.map((b) => b[0])), t = Math.min(...all.map((b) => b[1]));
    const r = Math.max(...all.map((b) => b[2])), btm = Math.max(...all.map((b) => b[3]));
    const z = Math.min(area.clientWidth / ((r - l) * 1.2 || 1), area.clientHeight / ((btm - t) * 1.25 || 1));
    return { x: (l + r) / 2 - area.clientWidth / 2 / z, y: (t + btm) / 2 - area.clientHeight / 2 / z, z };
  }

  function insideView(v) {
    const area = $("#kvm-canvas");
    return Object.values(groups).map(box).every(([l, t, r, b]) =>
      (l - v.x) * v.z >= 0 && (t - v.y) * v.z >= 0 && (r - v.x) * v.z <= area.clientWidth && (b - v.y) * v.z <= area.clientHeight);
  }

  const px = (v) => `${Math.round(v * 10) / 10}px`;

  function placeGroup(node, g) {
    const [l, t, r, b] = box(g);
    Object.assign(node.style, { left: px((l - view.x) * view.z), top: px((t - view.y) * view.z),
                                width: px((r - l) * view.z), height: px((b - t) * view.z) });
  }

  function renderCanvas(refit) {
    if (!config || mode !== "monitors") return;
    const area = $("#kvm-canvas");
    if (refit || !view || !insideView(view)) view = fit();
    if (!view) { area.replaceChildren(); return; }
    const nodes = Object.entries(groups).map(([name, g]) => {
      const b = bounds(g);
      const node = el("div", { class: "kvm-group" + (g.tile ? " tile" : ""), tabindex: 0, "data-name": name,
                               role: "button", "aria-label": `${name}: drag, or use the arrow keys, to move it` });
      for (const m of g.monitors) {
        const label = g.tile ? "no monitors reported" : `${m.name} ${m.pixel_width || m.width}×${m.pixel_height || m.height}`
          + (m.mirrors && m.mirrors.length ? `, also on ${m.mirrors.join(", ")}` : "");
        node.append(el("div", { class: "kvm-monitor", title: label, style:
          `left:${((m.x - b.x) / b.w) * 100}%;top:${((m.y - b.y) / b.h) * 100}%;width:${(m.width / b.w) * 100}%;height:${(m.height / b.h) * 100}%` },
          el("span", {}, label)));
      }
      node.append(el("strong", { class: "kvm-group-name" }, name), el("span", { class: "kvm-resize", title: "Drag to resize" }));
      placeGroup(node, g);
      return node;
    });
    const edges = (analysis ? analysis.edges : []).filter((e) => e.from < e.to).map((e) => {
      const [[x0, y0], [x1, y1]] = e.segment;
      const vertical = x0 === x1;
      return el("div", { class: "kvm-edge" + (e.outer ? "" : " inner") + (vertical ? " v" : " h"),
                         title: `The mouse crosses between ${e.from} and ${e.to} here`,
                         style: `left:${px((Math.min(x0, x1) - view.x) * view.z)};top:${px((Math.min(y0, y1) - view.y) * view.z)};`
                           + (vertical ? `height:${px(Math.abs(y1 - y0) * view.z)}` : `width:${px(Math.abs(x1 - x0) * view.z)}`) });
    });
    area.replaceChildren(...nodes, ...edges);
    renderNotes();
    renderGroupList();
    renderSetups();
  }

  function renderSetups() {
    const list = (config.layout && config.layout.setups) || [];
    const rows = list.map((s) => {
      const state = [s.active ? "in use" : "", s.current ? "matches what's connected" : ""].filter(Boolean).join(", ");
      return el("li", { class: "kvm-row" },
        el("span", { class: "kvm-setup-label" }, s.label),
        state ? el("span", { class: "form-note" }, `(${state})`) : "",
        s.active ? "" : el("button", { type: "button", onclick: (e) => save("layout", "/kvm/config/setups/use", { key: s.key }, e.target) }, "Use now"),
        el("button", { type: "button", onclick: (e) => {
          if (confirm(`Forget the layout for ${s.label}?`)) save("layout", "/kvm/config/setups/delete", { key: s.key }, e.target);
        } }, "Forget"));
    });
    $("#kvm-setups").replaceChildren(...(rows.length ? rows : [el("li", { class: "form-note" }, "None yet. Save the layout to keep it for the monitors connected now.")]));
  }

  function renderNotes() {
    const notes = [];
    const setup = config.layout && config.layout.setup;
    if (setup && !setup.known) {
      notes.push([(config.layout.setups || []).length
        ? `New setup (${setup.label}). This starts from the closest saved layout; arrange it and save to keep it for this setup.`
        : `Save to keep this layout for what's connected now (${setup.label}).`, false]);
    }
    // On a new setup, the note above already says the monitors differ.
    if (!setup || setup.known) for (const n of changed) notes.push([`${n}'s monitors changed since the layout was saved; it's drawn with the current ones. Save to use them.`, false]);
    if (analysis) {
      for (const e of analysis.errors) notes.push([e, true]);
      for (const w of analysis.warnings) notes.push([w, false]);
    }
    $("#kvm-layout-notes").replaceChildren(...notes.map(([text, bad]) => el("li", { class: bad ? "form-error" : "form-note" }, text)));
  }

  function renderGroupList() {
    $("#kvm-groups").replaceChildren(...Object.entries(groups).map(([name, g]) => {
      const choice = el("select", { "aria-label": `${name} size` },
        Object.entries(SIZE_LABELS).map(([v, label]) => el("option", {
          value: v, selected: v === g.size,
          disabled: (g.tile && v !== "manual") || (v === "physical" && !physicalScale(g)),
        }, label)));
      choice.addEventListener("change", () => {
        g.size = choice.value;
        g.scale = sizeScale(g);
        changedLayout();
      });
      const what = g.tile ? "no Bragi tray app has reported its monitors"
        : `${g.monitors.length} monitor${g.monitors.length === 1 ? "" : "s"}`;
      return el("li", { class: "kvm-row" }, el("strong", {}, name), el("span", { class: "form-note" }, what),
        el("label", { class: "kvm-size" }, "Size ", choice));
    }));
  }

  // --- moving

  function edgesOf(g) {
    const rs = [...rects(g), box(g)];
    return { xs: rs.flatMap((r) => [r[0], r[2]]), ys: rs.flatMap((r) => [r[1], r[3]]) };
  }

  // Moves g by the smallest step (under the snap distance) that lines one of
  // its edges up with another computer's.
  function snap(name, g) {
    const mine = edgesOf(g);
    const theirs = Object.entries(groups).filter(([n]) => n !== name).map(([, o]) => edgesOf(o));
    const limit = SNAP_PX / view.z;
    for (const axis of ["xs", "ys"]) {
      let best = null;
      for (const a of mine[axis]) for (const o of theirs) for (const b of o[axis]) {
        const d = b - a;
        if (Math.abs(d) <= limit && (best === null || Math.abs(d) < Math.abs(best))) best = d;
      }
      if (best !== null) g[axis === "xs" ? "x" : "y"] += best;
    }
  }

  function overlaps(name) {
    const mine = rects(groups[name]);
    return Object.entries(groups).some(([n, o]) => n !== name && rects(o).some((b) => mine.some((a) =>
      Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.5 && Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.5)));
  }

  let previewTimer = null;
  function changedLayout() {
    analysis = null;
    renderCanvas(false);
    clearTimeout(previewTimer);
    previewTimer = setTimeout(preview, 150);
  }

  async function preview() {
    if (mode !== "monitors") return;
    try {
      analysis = await call("POST", "/kvm/config/layout/preview", { layout: layoutBody() });
    } catch (err) {
      analysis = { edges: [], warnings: [], errors: [err.message] };
    }
    renderCanvas(false);
  }

  function layoutBody() {
    const out = {};
    for (const [name, g] of Object.entries(groups)) {
      out[name] = { x: Math.round(g.x * 100) / 100, y: Math.round(g.y * 100) / 100, scale: g.scale, size: g.size,
                    monitors: g.monitors.map(({ id, name: n, x, y, width, height }) => ({ id, name: n, x, y, width, height })) };
    }
    return { mode, groups: out };
  }

  const canvas = $("#kvm-canvas");
  canvas.addEventListener("pointerdown", (e) => {
    const node = e.target.closest(".kvm-group");
    if (!node || e.button !== 0) return;
    const name = node.dataset.name;
    const g = groups[name];
    const resizing = e.target.classList.contains("kvm-resize");
    const start = { px: e.clientX, py: e.clientY, x: g.x, y: g.y, scale: g.scale, w: box(g)[2] - box(g)[0] };
    node.setPointerCapture(e.pointerId);
    node.classList.add("moving");
    $("#kvm-canvas").querySelectorAll(".kvm-edge").forEach((n) => n.remove());
    const move = (ev) => {
      const dx = (ev.clientX - start.px) / view.z, dy = (ev.clientY - start.py) / view.z;
      if (resizing) {
        g.scale = Math.max(0.05, start.scale * Math.max(0.1, (start.w + dx) / start.w));
        g.size = "manual";
      } else {
        g.x = start.x + dx;
        g.y = start.y + dy;
        if (!ev.altKey) snap(name, g);
      }
      node.classList.toggle("overlap", overlaps(name));
      placeGroup(node, g);
    };
    const up = () => {
      node.removeEventListener("pointermove", move);
      node.removeEventListener("pointerup", up);
      node.removeEventListener("pointercancel", up);
      node.classList.remove("moving");
      changedLayout();
    };
    node.addEventListener("pointermove", move);
    node.addEventListener("pointerup", up);
    node.addEventListener("pointercancel", up);
    e.preventDefault();
  });
  canvas.addEventListener("keydown", (e) => {
    const node = e.target.closest(".kvm-group");
    const step = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    if (!node || !step) return;
    const g = groups[node.dataset.name];
    const d = (e.shiftKey ? 2 : 20) / view.z;
    g.x += step[0] * d;
    g.y += step[1] * d;
    if (!e.shiftKey) snap(node.dataset.name, g);
    e.preventDefault();
    changedLayout();
    $(`.kvm-group[data-name="${CSS.escape(node.dataset.name)}"]`).focus();
  });
  new ResizeObserver(() => { if (dialog.open && mode === "monitors") renderCanvas(true); }).observe(canvas);

  function setMode(next) {
    mode = next;
    for (const r of dialog.querySelectorAll('input[name="kvm-mode"]')) r.checked = r.value === mode;
    $("#kvm-grid-editor").hidden = mode !== "grid";
    $("#kvm-canvas-editor").hidden = mode !== "monitors";
    $("#kvm-unplace").hidden = mode !== "grid";
    $("#kvm-arrange").hidden = mode !== "monitors";
    if (mode === "monitors") {
      renderCanvas(true);
      preview();
    }
  }
  for (const r of dialog.querySelectorAll('input[name="kvm-mode"]')) r.addEventListener("change", () => setMode(r.value));
  $("#kvm-arrange").addEventListener("click", () => {
    arrangeLikeGrid();
    view = null;
    changedLayout();
  });

  // --- hotkeys -----------------------------------------------------------------

  // keystroke(KEYS) = ACTION, parsed into a form when it's one of these.
  const ACTIONS = [
    ["bragi", "Bragi action (next computer, keys through the Bragi apps, ...)"],
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
    if ((a = action.match(/^notify\((bragi-\d+)\)$/))) {
      return { kind: "bragi", keys, id: a[1], action: (config.hotkey_actions || {})[a[1]] || { type: "kvm_next" } };
    }
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
    if (kind === "bragi") return `keystroke(${keys}) = notify(${row.dataset.bragiId})`;
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
    // A Bragi action: the same choices as the mouse buttons (app/actions.py).
    const ba = h.action || { type: "kvm_next" };
    const bragiChoice = el("select", { class: "kvm-bragi", "aria-label": "Bragi action" },
      el("option", { value: "kvm_next", selected: ba.type === "kvm_next" }, "Next computer"),
      names().map((n) => el("option", { value: `switch:${n}`, selected: ba.type === "kvm_switch" && ba.computer === n }, `Switch to ${n}`)),
      el("option", { value: "kvm_keys", selected: ba.type === "kvm_keys" }, "Press keys on computers"));
    const bragiKeys = el("input", { type: "text", class: "kvm-send kvm-bragi-keys", value: ba.keys || "",
                                    placeholder: "Control+Shift+Alt+M", "aria-label": "Keys to press" });
    const bragiTargets = el("span", { class: "kvm-targets kvm-bragi-targets" }, names().map((n) => el("label", {},
      el("input", { type: "checkbox", value: n, checked: (ba.computers || []).includes(n) }), n)));
    row.dataset.bragiId = h.id || "";
    bragiChoice.addEventListener("change", () => show());
    const show = () => {
      const k = action.value;
      keys.hidden = k === "raw";
      target.hidden = k !== "switch";
      send.hidden = targets.hidden = k !== "send";
      bragiChoice.hidden = k !== "bragi";
      bragiKeys.hidden = bragiTargets.hidden = k !== "bragi" || bragiChoice.value !== "kvm_keys";
      raw.hidden = k !== "raw";
      if (k === "raw" && !raw.value) raw.value = line;
    };
    action.addEventListener("change", show);
    row.append(keys, action, target, send, targets, bragiChoice, bragiKeys, bragiTargets, raw, remove);
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
      .map((h) => pick(`${h.keys} (${h.kind === "switch" ? `switch to ${h.target}` : h.kind === "next" ? "next computer" : h.kind === "lock" ? "lock cursor" : h.kind === "bragi" ? "Bragi action" : "send keys"})`, h.keys));
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
    initGroups();
    analysis = null;
    setMode(mode);
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
  // The Bragi action of a hotkey row, and an id for it (bragi-N) if it has none yet.
  function bragiAction(row, used) {
    if (!row.dataset.bragiId) {
      let n = 1;
      while (used.has(`bragi-${n}`)) n++;
      row.dataset.bragiId = `bragi-${n}`;
    }
    used.add(row.dataset.bragiId);
    const v = row.querySelector(".kvm-bragi").value;
    if (v.startsWith("switch:")) return { type: "kvm_switch", computer: v.slice(7) };
    if (v === "kvm_keys") {
      return { type: v, keys: row.querySelector(".kvm-bragi-keys").value.trim(),
               computers: [...row.querySelectorAll(".kvm-bragi-targets input:checked")].map((i) => i.value) };
    }
    return { type: v };
  }

  const saveLayout = (e) => {
    const rows = [...dialog.querySelectorAll("#kvm-hotkeys li")];
    const used = new Set(rows.map((r) => r.dataset.bragiId).filter(Boolean));
    const hotkey_actions = {};
    for (const row of rows) {
      if (row.querySelector(".kvm-action").value === "bragi" && row.querySelector(".kvm-keys").value.trim()) {
        const action = bragiAction(row, used);
        hotkey_actions[row.dataset.bragiId] = action;
      }
    }
    const hotkeys = rows.map(hotkeyLine).filter(Boolean);
    const body = { cells: config.cells, hotkeys, hotkey_actions };
    // Saving hotkeys keeps the saved layout; saving the layout sends the one drawn.
    if (e.target.dataset.section === "layout") body.layout = layoutBody();
    save(e.target.dataset.section, "/kvm/config/layout", body, e.target);
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
      // The monitor layout needs the monitors before it's drawn.
      try {
        monitorView = await call("GET", "/kvm/monitors");
        renderMonitors(monitorView);
      } catch (err) {
        monitorView = null;
        $("#kvm-monitors").replaceChildren(el("p", { class: "form-error" }, err.message));
      }
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
