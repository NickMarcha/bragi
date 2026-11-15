// Passkeys (app/passkeys.py): the browser half of adding one and signing in
// with one. The server sends WebAuthn options as JSON with base64url
// strings; browsers want ArrayBuffers, and send credentials back the other
// way. Newer browsers do both conversions themselves; this falls back to
// doing them by hand.
window.BragiPasskeys = (() => {
  const supported = () => typeof window.PublicKeyCredential !== "undefined";

  const toBytes = (text) => {
    const b64 = text.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((text.length + 3) % 4);
    return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  };
  const toText = (buffer) => btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  function creationOptions(json) {
    if (PublicKeyCredential.parseCreationOptionsFromJSON) return PublicKeyCredential.parseCreationOptionsFromJSON(json);
    return {
      ...json,
      challenge: toBytes(json.challenge),
      user: { ...json.user, id: toBytes(json.user.id) },
      excludeCredentials: (json.excludeCredentials || []).map((c) => ({ ...c, id: toBytes(c.id) })),
    };
  }

  function requestOptions(json) {
    if (PublicKeyCredential.parseRequestOptionsFromJSON) return PublicKeyCredential.parseRequestOptionsFromJSON(json);
    return {
      ...json,
      challenge: toBytes(json.challenge),
      allowCredentials: (json.allowCredentials || []).map((c) => ({ ...c, id: toBytes(c.id) })),
    };
  }

  function credentialJSON(credential) {
    if (typeof credential.toJSON === "function") return credential.toJSON();
    const r = credential.response;
    const response = { clientDataJSON: toText(r.clientDataJSON) };
    if (r.attestationObject) {
      response.attestationObject = toText(r.attestationObject);
      if (r.getTransports) response.transports = r.getTransports();
    } else {
      response.authenticatorData = toText(r.authenticatorData);
      response.signature = toText(r.signature);
      if (r.userHandle) response.userHandle = toText(r.userHandle);
    }
    return { id: credential.id, rawId: toText(credential.rawId), type: credential.type, response,
             clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {} };
  }

  async function post(url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = null;
    try { data = await response.json(); } catch (_) { /* empty */ }
    if (!response.ok) throw new Error((data && typeof data.detail === "string" && data.detail) || `HTTP ${response.status}`);
    return data;
  }

  // A cancelled browser prompt isn't an error worth showing.
  const cancelled = (err) => err && (err.name === "NotAllowedError" || err.name === "AbortError");

  async function add(name) {
    const { token, options } = await post("/auth/passkeys/options");
    const credential = await navigator.credentials.create({ publicKey: creationOptions(options) });
    return post("/auth/passkeys", { token, name, credential: credentialJSON(credential) });
  }

  async function signIn(next) {
    const { token, options } = await post("/auth/passkey-login/options");
    const credential = await navigator.credentials.get({ publicKey: requestOptions(options) });
    return post("/auth/passkey-login", { token, next, credential: credentialJSON(credential) });
  }

  return { supported, add, signIn, cancelled };
})();
