# Bragi

A small web UI for managing [sagepi](https://github.com/NickMarcha/deck-assistant)'s
network audio bridge: which peers are connected, per-peer volume in each
direction, and adding/removing Roc peers without hand-editing PipeWire
config.

Named after the Norse god of poetry and eloquence — fitting for something
that pushes voices around a network.

## What it does

- Auto-detects any USB headset physically plugged into `sagepi` (paired by
  ALSA card id, no registry needed — whatever's plugged in *is* the list)
  and shows a **Speakers** slider (+ mute + L/R balance) and a **Mic**
  slider (+ mute) for it. This card polls itself every few seconds so it
  stays in sync when the headset's own hardware volume knob is turned —
  USB headsets report hardware volume changes straight through to
  PipeWire, this just surfaces it in the UI.
- Lists every configured peer (both hand-configured ones already running on
  `sagepi` and ones added through this UI) with two independent volume
  sliders + mute toggles per peer:
  - **Mic → peer** — how loud `sagepi`'s headset mic sounds to that peer.
  - **Peer → headset** — how loud that peer's incoming audio is in
    `sagepi`'s own mixed headset output, with an L/R balance slider too
    (this direction is the stereo one — mic-direction audio is mono, so it
    only gets volume + mute).
- Add/remove **Roc** peers (Linux/`sagedeck`/`sage-dev`-style) from the UI.
  New peers are hot-loaded into the live PipeWire graph immediately *and*
  written to a dedicated config file, so they also survive a
  `pipewire.service` restart or reboot without Bragi needing to be running.
- Existing hand-configured peers (seeded to match the current deployment —
  `sagedeck`, `sage-dev` via Roc; `sage` via VBAN) show up too, with working
  volume control, but can't be removed from the UI — see Known Limitations.

Balance is implemented as raw per-channel PipeWire volume (`channelVolumes`
via `pw-cli set-param`) since PipeWire has no native pan control — Bragi
remembers each pannable node's intended balance in `data/balance.yaml`
(keyed by node name) and reapplies it any time the plain volume slider
changes too, so the two controls don't fight each other.

## Architecture

Bragi is a Python/FastAPI app that never talks to the PipeWire socket
directly — it shells out to `pw-dump`, `wpctl`, and `pw-cli`, the same
tools you'd use by hand. PipeWire itself, WirePlumber, the Roc modules, and
VBAN all stay bare-metal on the host — only this UI runs in Docker.

```
┌─────────────── sagepi (bare metal) ───────────────┐
│  PipeWire + WirePlumber + Roc modules + VBAN       │
│  ~/.config/pipewire/pipewire.conf.d/*.conf         │
└───────────────────┬─────────────────────────────--┘
                     │ /run/user/1000 (socket) bind-mounted
                     │ pipewire.conf.d bind-mounted (write access)
┌────────────────────▼───────────────────────────---┐
│  Docker: bragi (this repo)                         │
│  FastAPI + htmx, shells out to pw-dump/wpctl/pw-cli│
└─────────────────────────────────────────────────--┘
```

See `docker-compose.yml` for the exact bind mounts. In production this
runs as a Komodo git-sourced Stack targeting the `sagepi` Server (see
deck-assistant issue #061) rather than plain `docker compose`, but the
compose file is what the Stack mirrors.

## Known limitations (v1)

- **VBAN peers can't be added/removed from the UI.** `vban_emitter`/
  `vban_receptor` always register as a PipeWire client literally named
  `vban` — Bragi tells the two directions apart by stream class
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
  be removed from the UI at all — only their volume is controllable. Edit
  `~/.config/pipewire/pipewire.conf.d/` on `sagepi` directly for those.

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
- `~/.local/share/bragi/data/` for the persisted peer registry.
