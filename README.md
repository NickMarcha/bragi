# Bragi

A small web UI for managing a Raspberry Pi's network audio bridge: which
peers are connected, per-peer volume in each direction, and adding or
removing Roc peers without hand-editing PipeWire config. The Pi it runs on
is `sagepi`, part of the [deck-assistant](https://github.com/NickMarcha/deck-assistant)
fleet.

Named after the Norse god of poetry and eloquence, fitting for something
that pushes voices around a network.

## Docs

- [`docs/audio-bridge.md`](docs/audio-bridge.md) — the PipeWire graph, the
  Roc and VBAN transports, and the parts that took several tries to make
  stable. Read this to understand what Bragi is controlling.
- [`docs/deployment.md`](docs/deployment.md) — how this runs on `sagepi`
  (Komodo git-sourced Stack, `tailscale serve`, the deploy webhook).
- [`docs/realtime-control-plane.md`](docs/realtime-control-plane.md) — how
  live updates work and the race conditions behind the odd-looking guards.
- [`docs/backlog.md`](docs/backlog.md) — known bugs and deferred work.

## What it does

A Voicemeeter-style mixer console, updated live over a WebSocket instead of
polling - see [`docs/realtime-control-plane.md`](docs/realtime-control-plane.md)
for how that actually works and the (non-trivial) history of getting it to
not visibly glitch.

- **Local headsets**: any USB headset physically plugged into `sagepi` is
  auto-detected (paired by ALSA card id, no registry needed - whatever's
  plugged in *is* the list) and gets a **Speakers** fader (+ mute) and a
  **Mic** fader (+ mute). No balance control on these - see Known
  Limitations, it's a hardware constraint, not a missing feature.
  - **Enable/disable**: flips the ALSA card's profile to `off` and back,
    fully releasing the USB device (no PipeWire nodes, no USB bandwidth)
    instead of just muting it. For running two headsets but only using one
    at a time without either drawing power/bandwidth unnecessarily.
  - **Physical volume knob**: these headsets' knobs send standard
    `KEY_VOLUMEUP`/`KEY_VOLUMEDOWN` media keys over a separate USB HID
    "Consumer Control" interface - exactly like a keyboard volume key. On a
    normal desktop, the running session's media-key handler catches that;
    `sagepi` is headless with nothing to catch it, so Bragi does
    (`app/knob_watcher.py`), translating knob turns into real volume steps.
- **Peers**: every configured peer (hand-configured ones already running
  on `sagepi`, or ones added through the UI) gets two faders:
  - **Mic → peer** - how loud `sagepi`'s headset mic sounds to that peer.
    Mono, so volume + mute only.
  - **Peer → headset** - how loud that peer's incoming audio is in
    `sagepi`'s own mixed headset output. Stereo, so it also gets an L/R
    balance pad (drag horizontally to pan; right-click either control to
    reset - fader to unity, pad to centered).
- **Add/remove Roc peers** (Linux/`sagedeck`/`sage-dev`-style) from the UI.
  New peers are hot-loaded into the live PipeWire graph immediately *and*
  written to a dedicated config file, so they survive a
  `pipewire.service` restart or reboot without Bragi needing to be running.
  Hand-configured peers (seeded to match the current deployment -
  `sagedeck`, `sage-dev` via Roc; `sage` via VBAN) show up too, with
  working volume control, but can't be removed from the UI.
- **Per-card status dot** (headsets and peers): green/amber/red/grey next
  to each name. For Roc peers running the Bragi Client tray app
  (`sage-dev`, `sagedeck` - see `client/`), this is *real* reachability - a
  live WebSocket the tray app holds open to `app/main.py`'s
  `/ws/peer/{name}` for as long as its Roc link is enabled
  (`app/peer_presence.py`), not just "does a PipeWire node exist locally."
  That distinction matters: a Roc module can exist and look fine on both
  ends even when the other side is completely gone, since Roc runs over
  connectionless UDP - confirmed directly while building this (`pw-dump`
  node state and `pw-top`'s RATE/status didn't change at all between a
  genuinely-connected and a deliberately-disabled peer). For the VBAN
  `sage` peer (no tray app) and any custom peer without a client, the dot
  falls back to local PipeWire node presence, same as headsets.
- **%/dB toggle** and a **connection status indicator** in the top corner.

Balance is implemented as raw per-channel PipeWire volume (`channelVolumes`
via `pw-cli set-param`) since PipeWire has no native pan control - Bragi
remembers each pannable node's intended volume+balance pair in
`data/balance.yaml` (keyed by node name) and reapplies both together on
every change, so the two controls don't fight each other (see
`app/audio_state.py`).

## Architecture

Bragi is a Python/FastAPI app that never talks to the PipeWire socket
directly - it shells out to `pw-dump`, `wpctl`, and `pw-cli`, the same
tools you'd use by hand. PipeWire itself, WirePlumber, the Roc modules, and
VBAN all stay bare-metal on the host - only this UI runs in Docker.

```
┌─────────────── sagepi (bare metal) ─────────────────┐
│  PipeWire + WirePlumber + Roc modules + VBAN         │
│  /dev/input/eventN (headset volume-knob HID)         │
│  ~/.config/pipewire/pipewire.conf.d/*.conf           │
└───────────────────┬───────────────────────────────--┘
                     │ /run/user/1000 (socket) bind-mounted
                     │ /dev/input device-mapped (cgroup access, not just visible)
                     │ pipewire.conf.d bind-mounted (write access)
┌────────────────────▼─────────────────────────────---┐
│  Docker: bragi (this repo)                           │
│  FastAPI + WebSocket, shells out to pw-dump/wpctl/   │
│  pw-cli. Background: pw-mon watcher (hardware/other- │
│  client changes) + knob_watcher (HID volume keys).   │
└───────────────────────────────────────────────────--┘
```

The dashboard's *initial* load is still plain server-rendered HTML
(`GET /`), but every live update after that goes over one WebSocket per
browser tab (`app/ws.py`) - see the linked doc for the actual design
(single-flight-per-control worker, client-timestamp-based ordering, why a
"just poll every few seconds" version was replaced). Add/remove-peer stays
a plain form POST (htmx) - infrequent, no reason to put it on the socket.

See `docker-compose.yml` for the exact bind mounts. In production this
runs as a Komodo git-sourced Stack targeting the `sagepi` Server rather
than plain `docker compose`, but the compose file is what the Stack
mirrors. See [`docs/deployment.md`](docs/deployment.md).

## Known limitations (v1)

These are the design constraints. For open bugs and deferred features, see
[`docs/backlog.md`](docs/backlog.md).

- **Headset balance doesn't exist, on purpose.** WirePlumber's
  alsa-monitor treats the physical hardware mixer as authoritative and
  reverts any software per-channel (`channelVolumes`) write on a real ALSA
  sink within milliseconds - confirmed live, not a bug to fix. Balance only
  holds on software nodes (the Roc/VBAN peer streams), which is why only
  peer "incoming" strips have a pad.
- **VBAN peers can't be added/removed from the UI.** `vban_emitter`/
  `vban_receptor` always register as a PipeWire client literally named
  `vban` - Bragi tells the two directions apart by stream class
  (`Stream/Input/Audio` = mic going out, `Stream/Output/Audio` = incoming
  playback), which only works unambiguously with a single VBAN peer. A
  second VBAN peer would need a different disambiguation strategy
  (probably: give up on PipeWire-level naming and track VBAN peers by PID
  or by wrapping each in a differently-named systemd service).
- **Removing a peer added earlier this session, after Bragi itself
  restarts, doesn't hot-unload it.** The persisted config file is still
  correctly rewritten without that peer, but the *live* PipeWire modules
  linger until the next `pipewire.service` restart, because the module IDs
  needed to unload them live only in Bragi's process memory, not on disk.
- Hand-configured peers (seeded in `app/peers.py`'s `_seed_peers()`) can't
  be removed from the UI at all - only their volume is controllable. Edit
  `~/.config/pipewire/pipewire.conf.d/` on `sagepi` directly for those.
- **Not designed for multiple browser tabs/clients moving the same control
  at once.** Broadcasts sync everyone's *display*, but the timestamp-based
  ordering that keeps a drag from visibly glitching (see the architecture
  doc) deliberately only orders one client's own messages against
  itself - a second client dragging the same fader at the same time isn't
  a designed-for case.
- **The realtime control plane is still under live observation.** It went
  through several rounds of "looks fixed" that turned out not to be under
  real dragging, each one root-caused by actually reproducing it (see the
  architecture doc's history section) rather than reasoning about the code -
  worth being skeptical of "this is definitely fixed now" until it's held
  up under more real-world use than one session's worth of testing.

## Local development

Needs a real PipeWire session to do anything useful (`pw-dump`/`wpctl`
must resolve against a running daemon), so this is realistically only
testable on `sagepi` itself or another PipeWire-based Linux machine:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Deployment

```bash
docker compose up -d --build
```

expects, on the host:
- User `sage` (uid 1000) with an active PipeWire session at
  `/run/user/1000/pipewire-0`.
- `~/.config/pipewire/pipewire.conf.d/` writable by that user.
- `~/.local/share/bragi/data/` for the persisted peer registry + balance state.
- `/dev/input/*` readable, for the knob watcher (see `app/knob_watcher.py`).
  Needs the host's `input` group's GID added via `group_add` (996 on
  sagepi - `getent group input` to confirm on any other host) *and* the
  device mapped via `devices:`, not just bind-mounted via `volumes:` - a
  plain bind mount only makes the nodes visible in the container's
  filesystem, it does not grant the cgroup permission to actually open
  them (surfaces as `PermissionError: [Errno 1]` / EPERM, not the EACCES a
  real Unix permission mismatch would give - confirmed live, see
  `docker-compose.yml`'s comments on this).
