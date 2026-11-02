# API and access control

Two features that share one middleware: a password for the web UI, and API
keys that let other apps read and change volumes. The API's own usage guide
is served by Bragi itself at `/api/v1/help` (source: `HELP` in
`app/api.py`), so it stays next to the code it describes. This page covers
how access works and why it is built this way.

## Who can reach what

`app/access.py`'s `AccessMiddleware` puts every request in one of four
groups:

| Group | Paths | Needs |
|---|---|---|
| public | `/static/*`, `/login`, `/auth/login`, `/api/v1/help` | nothing |
| peer | `/ws/peer/{name}`, `/api/peers/register`, `/api/peers/register-desktop`, `/api/peers/{name}/streams` | nothing |
| api | `/api/v1`, `/api/v1/*` | an API key, always |
| web | everything else, including `/ws` | a session cookie, but only once a password is set |

The peer group stays open because the Android app and the tray app's
presence heartbeat have no way to log in. They were open before this, and
Tailscale is still their boundary. If that ever needs closing, they would
need API keys of their own.

`/ws` also takes an API key (`?token=`), so another app can use the same
live socket the dashboard does. A key that is present but wrong is refused
even when no password is set. The tray app's volume window uses `/ws`, so
once a password is set it needs `BRAGI_API_KEY` in its env file (see
`client/systemd/roc-link.env.example`).

API keys are required even without a password. That keeps "an app can
change my volume" an explicit choice made in the Security dialog, and a
connection keeps working when a password is added later.

## Sessions

Logging in sets two `HttpOnly`, `SameSite=Lax` cookies (`Secure` when the
request came over HTTPS, which behind `tailscale serve` is read from
`X-Forwarded-Proto`):

- `bragi_access`: `<session id>.<expiry>.<HMAC>`, valid 15 minutes. Checked
  with one HMAC plus a lookup that the session still exists, so logging
  out or changing the password takes effect at once, not 15 minutes later.
- `bragi_refresh`: 32 random bytes, stored only as a SHA-256 hash in
  `data/auth.yaml`. Lives 400 days (Chrome's cap) and is renewed on every
  rotation, so a browser used at least once every 400 days never has to
  log in again.

When the access cookie has expired, the middleware trades the refresh
cookie for a new pair on whatever request comes next and adds the new
cookies to that response. No client code takes part.

Rotation has two edge cases, both in `auth.refresh_session`:

- A page load sends several requests at once, all with the same expired
  access cookie and the same refresh cookie. The first one rotates. The
  rest present a refresh token that is now the session's `previous` one.
  Within 60 seconds of the rotation that is accepted and gets a new access
  token only, with no new refresh cookie, so it cannot overwrite the one
  the first response set.
- The same `previous` token later than that means someone kept a copy, or
  the browser lost a response. The session is ended and the browser logs
  in again.

WebSocket handshakes cannot set cookies. Rotating there would retire a
token the browser still holds, and its next request would look like
theft. So `/ws` accepts a valid refresh cookie without rotating it
(`auth.peek_refresh`), and `ws.js` calls `GET /auth/session` before every
connect anyway, which refreshes the cookies through the normal path and
sends the tab to `/login` on a 401.

Changing the password ends every session; the browser that changed it gets
a new one. Removing the password ends every session too.

Cookie-authenticated writes from another origin get a 403: `SameSite=Lax`
already keeps the cookie off cross-site POSTs, and the `Origin` check
covers browsers that do not.

## The password

Hashed with scrypt (`n=2^14, r=8, p=1`), about 50 ms on a Pi 4. PBKDF2 at a
comparable strength takes about a second there. Minimum length is 4
characters; the user asked for a simple password.

If it is forgotten, stop Bragi, set `password: null` in
`~/.local/share/bragi/data/auth.yaml` on the host, and start it again. That
reopens the web UI and keeps the API keys.

## `data/auth.yaml`

```yaml
password: scrypt$16384$8$1$<salt>$<hash>   # or null
secret: <base64>          # HMAC key for access tokens, made on first login
sessions:
  <session id>:
    created: 1793500000
    last_used: 1793500000
    user_agent: Mozilla/5.0 ...
    current: <sha256 of the refresh token>
    previous: <sha256 of the one before>
    rotated_at: 1793500000
api_keys:
  - id: 3fa2c1d0
    name: stream-deck
    prefix: bragi_Xy12ab       # shown in the dialog to tell keys apart
    hash: <sha256 of the key>
    created: 1793500000
    last_used: 1793500000      # written at most once a minute
```

Written atomically with mode 600. The middleware reads it on every request,
so `auth._load` caches it by mtime.

## API writes and the control plane

`PATCH /api/v1/devices/.../controls/...` goes through `ws.set_control`,
which applies the change at once instead of through `_throttled_apply`: an
API call is one discrete request whose caller waits for the result, not a
drag. It marks the control in `_last_applied` like a slider tick, so the
pw-mon watcher's echo of the same write is dropped (see
[`realtime-control-plane.md`](realtime-control-plane.md)), and it
broadcasts the read-back control to every socket. That read-back is also
the HTTP response.

The broadcast carries `ts: null`. The dashboard always shows a `null`-ts
broadcast unless the user is mid-drag on that same fader.
