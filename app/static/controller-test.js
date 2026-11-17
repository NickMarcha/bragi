// The controller test (app/gamepad.py): what the box reads from a controller,
// live, and a Vibrate button. Opened from the Bluetooth dialog's controller
// list. While it's open the page asks /gamepads/watch every 5 s, and the box
// sends "gamepad_state" on the websocket (ws.js passes it on as a
// "bragi:gamepad" event).
(() => {
  const dialog = document.getElementById("controller-test-dialog");
  if (!dialog) return;

  const BUTTONS = [["a", "A"], ["b", "B"], ["x", "X"], ["y", "Y"], ["lb", "LB"], ["rb", "RB"],
                   ["back", "View"], ["start", "Menu"], ["guide", "Xbox"], ["ls", "L stick"], ["rs", "R stick"],
                   ["p1", "P1"], ["p2", "P2"], ["share", "Share"]];
  const $ = (sel) => dialog.querySelector(sel);
  let current = null; // controller id
  let timer = null;

  function say(text, isError = false) {
    const node = $("[data-ct-msg]");
    node.textContent = text || "";
    node.className = isError ? "form-error" : "form-note";
    node.hidden = !text;
  }

  async function call(url, body) {
    const response = await fetch(url, { method: "POST", credentials: "same-origin" });
    let data = null;
    try { data = await response.json(); } catch (_) { /* empty */ }
    if (!response.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `HTTP ${response.status}`);
    return data;
  }

  function build() {
    $("#ct-buttons").replaceChildren(...BUTTONS.map(([key, label]) => {
      const chip = document.createElement("span");
      chip.className = "ct-chip";
      chip.dataset.button = key;
      chip.textContent = label;
      return chip;
    }));
  }

  // Sticks come as -32768..32767 (Y down), triggers 0..255, d-pad -1/0/1.
  function stick(node, x, y) {
    const dot = node.querySelector(".ct-dot");
    dot.style.left = `${50 + (x / 32768) * 45}%`;
    dot.style.top = `${50 + (y / 32768) * 45}%`;
    node.title = `${x}, ${y}`;
  }

  function show(state) {
    const pressed = new Set(state.buttons || []);
    for (const chip of dialog.querySelectorAll("[data-button]")) chip.classList.toggle("on", pressed.has(chip.dataset.button));
    stick($("#ct-left"), state.lx || 0, state.ly || 0);
    stick($("#ct-right"), state.rx || 0, state.ry || 0);
    for (const [id, value] of [["#ct-lt", state.lt], ["#ct-rt", state.rt]]) {
      $(id).style.width = `${((value || 0) / 255) * 100}%`;
      $(id).parentElement.title = String(value || 0);
    }
    const pad = { up: state.dy < 0, down: state.dy > 0, left: state.dx < 0, right: state.dx > 0 };
    for (const chip of dialog.querySelectorAll("[data-dpad]")) chip.classList.toggle("on", pad[chip.dataset.dpad]);
  }

  async function watch() {
    try {
      const { controllers } = await call("/gamepads/watch");
      const mine = controllers.find((c) => c.id === current);
      if (!mine) {
        say("The controller isn't connected to the box any more.", true);
        return;
      }
      $("#ct-name").textContent = mine.name;
      show(mine);
    } catch (err) {
      say(err.message, true);
    }
  }

  async function open(id) {
    current = id;
    say("");
    build();
    show({});
    dialog.showModal();
    await watch();
    clearInterval(timer);
    timer = setInterval(watch, 5000);
  }

  function close() {
    clearInterval(timer);
    timer = null;
    current = null;
    dialog.close();
  }

  document.addEventListener("bragi:gamepad", (e) => {
    if (current && e.detail.id === current) show(e.detail);
  });
  // Delegated: the Bluetooth dialog's body is re-rendered by htmx.
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-controller-test]");
    if (button) open(button.dataset.controllerTest);
  });
  for (const button of dialog.querySelectorAll("[data-rumble]")) {
    button.addEventListener("click", async () => {
      if (!current) return;
      button.disabled = true;
      try {
        await call(`/gamepads/${encodeURIComponent(current)}/rumble?strength=${button.dataset.rumble}`);
        say("");
      } catch (err) {
        say(err.message, true);
      } finally {
        setTimeout(() => { button.disabled = false; }, 600);
      }
    });
  }
  $("#ct-close").addEventListener("click", close);
  dialog.addEventListener("close", () => { clearInterval(timer); current = null; });
})();
