// The Security dialog: set, change or remove the web UI password, log out
// other browsers, and manage API keys. Talks to app/access.py's /auth/*
// endpoints with JSON; the session cookies themselves are HttpOnly and
// never touched here.
(() => {
  const dialog = document.getElementById("security-dialog");
  const openBtn = document.getElementById("security-toggle");
  if (!dialog || !openBtn) return;

  const passwordForm = document.getElementById("password-form");
  const passwordError = passwordForm.querySelector(".form-error");
  const keyList = document.getElementById("api-keys");
  const keyForm = document.getElementById("api-key-form");
  const keyError = dialog.querySelector("[data-api-error]");
  const newKey = document.getElementById("api-key-new");

  async function call(method, url, body) {
    const response = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (response.status === 401) {
      location.href = "/login";
      throw new Error("Log in first");
    }
    let data = null;
    try { data = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok) {
      let message = "Something went wrong.";
      if (data && typeof data.detail === "string") message = data.detail;
      else if (data && Array.isArray(data.detail) && data.detail[0]) message = data.detail[0].msg;
      throw new Error(message);
    }
    return data;
  }

  function showError(el, message) {
    el.textContent = message;
    el.hidden = !message;
  }

  function when(seconds) {
    if (!seconds) return "never";
    return new Date(seconds * 1000).toLocaleString();
  }

  function renderPasswordState(passwordSet, tailscaleOnly = false) {
    dialog.querySelectorAll("[data-needs-password]").forEach((el) => { el.hidden = !passwordSet; });
    dialog.querySelector("[data-password-state]").textContent = passwordSet
      ? "A password is set. Browsers stay logged in until they log out."
      : tailscaleOnly
        ? "No password: only passkeys and trusted Tailscale accounts get in. A password is an optional last resort."
        : "No password: anyone who can reach this page can use it.";
    passwordForm.querySelector('button[type="submit"]').textContent = passwordSet ? "Change password" : "Set password";
  }

  function renderKeys(keys) {
    keyList.replaceChildren();
    if (!keys.length) {
      const empty = document.createElement("li");
      empty.className = "form-note";
      empty.textContent = "No API connections yet.";
      keyList.append(empty);
      return;
    }
    for (const key of keys) {
      const item = document.createElement("li");
      const text = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = key.name;
      const meta = document.createElement("span");
      meta.className = "form-note";
      meta.textContent = `${key.prefix}… · added ${when(key.created)} · last used ${when(key.last_used)}`;
      text.append(name, meta);
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.textContent = "Revoke";
      revoke.addEventListener("click", async () => {
        if (!confirm(`Revoke "${key.name}"? Apps using it lose access at once.`)) return;
        try {
          await call("DELETE", `/auth/api-keys/${encodeURIComponent(key.id)}`);
          await refresh();
        } catch (err) { showError(keyError, err.message); }
      });
      item.append(text, revoke);
      keyList.append(item);
    }
  }

  // Tailscale sign-in: trusted accounts get in without a password when
  // Bragi is opened through its Tailscale address (app/auth.py).
  const tailscaleList = document.getElementById("tailscale-logins");
  const tailscaleTrust = document.getElementById("tailscale-trust");
  const tailscaleState = dialog.querySelector("[data-tailscale-state]");
  const tailscaleError = dialog.querySelector("[data-tailscale-error]");

  function renderTailscale(current, trusted) {
    tailscaleList.replaceChildren();
    for (const login of trusted) {
      const item = document.createElement("li");
      const text = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = login;
      const meta = document.createElement("span");
      meta.className = "form-note";
      meta.textContent = login === current ? "this browser's account" : "trusted";
      text.append(name, meta);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        if (!confirm(`Stop letting ${login} in without a password?`)) return;
        try {
          await call("POST", "/auth/tailscale/untrust", { login });
          await refresh();
        } catch (err) { showError(tailscaleError, err.message); }
      });
      item.append(text, remove);
      tailscaleList.append(item);
    }
    tailscaleTrust.hidden = !current || trusted.includes(current);
    if (current) tailscaleTrust.textContent = `Let ${current} in without a password`;
    tailscaleState.textContent = !current
      ? "This browser didn't come through Tailscale, so it can't be added from here."
      : trusted.length
        ? "These accounts get in without a password when they open Bragi through Tailscale. Everyone else needs the password."
        : `You're signed in to Tailscale as ${current}. Add it and only trusted accounts (or the password, if set) get in.`;
  }

  tailscaleTrust.addEventListener("click", async () => {
    showError(tailscaleError, "");
    try {
      await call("POST", "/auth/tailscale/trust");
      await refresh();
    } catch (err) { showError(tailscaleError, err.message); }
  });

  // Passkeys (static/passkeys.js does the WebAuthn part).
  const passkeyList = document.getElementById("passkey-list");
  const passkeyAdd = document.getElementById("passkey-add");
  const passkeyError = dialog.querySelector("[data-passkey-error]");

  function renderPasskeys(list) {
    passkeyList.replaceChildren();
    if (!list.length) {
      const empty = document.createElement("li");
      empty.className = "form-note";
      empty.textContent = "No passkeys yet.";
      passkeyList.append(empty);
    }
    for (const passkey of list) {
      const item = document.createElement("li");
      const text = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = passkey.name;
      const meta = document.createElement("span");
      meta.className = "form-note";
      meta.textContent = `${passkey.rp_id} · added ${when(passkey.created)} · last used ${when(passkey.last_used)}`;
      text.append(name, meta);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        if (!confirm(`Remove the passkey "${passkey.name}"? It can't sign in here any more.`)) return;
        try {
          await call("DELETE", `/auth/passkeys/${encodeURIComponent(passkey.id)}`);
          await refresh();
        } catch (err) { showError(passkeyError, err.message); }
      });
      item.append(text, remove);
      passkeyList.append(item);
    }
    if (!window.BragiPasskeys || !BragiPasskeys.supported()) {
      passkeyAdd.disabled = true;
      showError(passkeyError, "This browser can't create passkeys.");
    }
  }

  passkeyAdd.addEventListener("click", async () => {
    showError(passkeyError, "");
    const name = prompt("Name this passkey, e.g. Bitwarden", "Bitwarden");
    if (name === null) return;
    try {
      await BragiPasskeys.add(name);
      await refresh();
    } catch (err) {
      if (!BragiPasskeys.cancelled(err)) showError(passkeyError, err.message);
    }
  });

  async function refresh() {
    const settings = await call("GET", "/auth/settings");
    renderPasswordState(settings.password_set,
      (settings.tailscale_logins || []).length > 0 || (settings.passkeys || []).length > 0);
    renderKeys(settings.api_keys);
    renderTailscale(settings.tailscale_login, settings.tailscale_logins || []);
    renderPasskeys(settings.passkeys || []);
  }

  openBtn.addEventListener("click", async () => {
    showError(passwordError, "");
    showError(keyError, "");
    newKey.hidden = true;
    dialog.showModal();
    try { await refresh(); } catch (err) { showError(keyError, err.message); }
  });
  document.getElementById("security-close").addEventListener("click", () => dialog.close());

  passwordForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    showError(passwordError, "");
    const form = new FormData(passwordForm);
    try {
      await call("POST", "/auth/password", {
        new_password: form.get("new_password"),
        current_password: form.get("current_password") || null,
      });
      passwordForm.reset();
      await refresh();
      showError(passwordError, "");
      dialog.querySelector("[data-password-state]").textContent =
        "Password saved. Other browsers have to log in again.";
    } catch (err) { showError(passwordError, err.message); }
  });

  document.getElementById("password-remove").addEventListener("click", async () => {
    showError(passwordError, "");
    const current = passwordForm.querySelector('input[name="current_password"]').value;
    if (!confirm("Remove the password? Anyone who can reach this page can then use it.")) return;
    try {
      await call("DELETE", "/auth/password", { current_password: current });
      passwordForm.reset();
      await refresh();
    } catch (err) { showError(passwordError, err.message); }
  });

  document.getElementById("sessions-revoke").addEventListener("click", async () => {
    try {
      const result = await call("POST", "/auth/sessions/revoke-others");
      dialog.querySelector("[data-password-state]").textContent =
        `Logged out ${result.ended} other browser${result.ended === 1 ? "" : "s"}.`;
    } catch (err) { showError(passwordError, err.message); }
  });

  keyForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    showError(keyError, "");
    const name = new FormData(keyForm).get("name");
    try {
      const created = await call("POST", "/auth/api-keys", { name });
      keyForm.reset();
      newKey.querySelector("code").textContent = created.key;
      document.getElementById("api-key-copy").textContent = "Copy";
      newKey.hidden = false;
      await refresh();
    } catch (err) { showError(keyError, err.message); }
  });

  document.getElementById("api-key-copy").addEventListener("click", async () => {
    const text = newKey.querySelector("code").textContent;
    try {
      await navigator.clipboard.writeText(text);
      document.getElementById("api-key-copy").textContent = "Copied";
    } catch (_) {
      // Clipboard needs a secure context; select it for a manual copy instead.
      const range = document.createRange();
      range.selectNodeContents(newKey.querySelector("code"));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }
  });
})();
