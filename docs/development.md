# Development

## Stack

Python 3.14, FastAPI, Jinja2 templates, htmx for two form posts, and one
hand-written `ws.js` for everything live. No build step, no bundler, no
frontend framework. Keep it that way: inline SVG icons, vanilla JS, Jinja
partials.

Dependencies are in `requirements.txt` (five packages). The audio work is
all subprocess calls to `pw-dump`, `wpctl`, `pw-cli`, and `pw-cat`, so
there is no PipeWire binding to install.

## Running it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

It needs a real PipeWire session to do anything. `pw-dump` and `wpctl` have
to resolve against a running daemon, so in practice you develop on `sagepi`
itself or another PipeWire-based Linux machine with a headset and at least
one peer configured. Against a machine with no PipeWire the app starts but
every card is empty.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

They run anywhere - no PipeWire needed. `tests/fake_pipewire.py` replaces
`pipewire._run`, the single place the app shells out, with a fake host
modelling the graph as it actually stands on `sagepi` (captured from a live
`pw-dump`): three peers, one enabled headset, one disabled one. Card
enable/disable is modelled faithfully, including the part that makes it
awkward - a card set to profile "off" loses its Sink/Source nodes entirely.

The suite exists mostly to hold a **performance** contract, because the
thing that made this dashboard slow was never logic, it was subprocess
count. A `pw-dump` costs ~100ms on the Pi 4 (85ms to run, 17ms to parse
495KB of JSON) and a `wpctl get-volume` ~50ms, so the tests assert on
`session.count("pw-dump")` as directly as they assert on behaviour. Before
those assertions existed, one click of the enable/disable button cost 76
`pw-dump` calls and took 9.7 seconds.

What is covered, and where:

| File | Holds |
|---|---|
| `test_headset_toggle.py` | Enable/disable: one dump per click, disabled cards stay discoverable, and enabling never writes "off" over a card it has no profile to restore. |
| `test_watcher_coalescing.py` | A pw-mon burst costs one dump no matter how many ids it carries, still suppresses echoes of a client's own action, and still lets a hardware knob turn through. |
| `test_level_meter.py` | The dB mapping against a known -20dBFS signal, one WebSocket frame per tick rather than one per meter, and a stopped capture falling to zero instead of freezing. |
| `test_dashboard_state.py` | `build_state()` and the fader/mute/balance actions, including client-timestamp ordering. |
| `test_connection.py` | A new tab gets its state snapshot first, and connecting wakes the meters. |

What tests do **not** cover, and cannot: the realtime control plane went
through several rounds of "looks fixed" that were only ever settled by
reproducing the glitch with a real browser drag against live hardware (see
[`realtime-control-plane.md`](realtime-control-plane.md)). Timing bugs of
that shape still need the real thing:

- Volume, mute, balance: move the control, confirm `wpctl status` /
  `pw-dump` reflects it, confirm a second browser tab shows the same.
- Peer add/remove: check `peers.yaml`, the generated
  `70-bragi-peers.conf`, and the live graph (`pw-dump | grep -i roc`).
- Control-plane changes: 8 to 15 consecutive real-drag reproductions, per
  the method in `realtime-control-plane.md`.

## The `app/` modules

| Module | Lines | What it owns |
|---|---|---|
| `main.py` | ~100 | The FastAPI app: routes, static mount, templates, and the lifespan that starts three background tasks (`watcher.watch`, `knob_watcher.watch`, `level_meter.supervise`). |
| `pipewire.py` | ~230 | The only place that shells out to `pw-dump` / `wpctl` / `pw-cli`. `dump()` returns a `Graph` (the Audio nodes *and* the ALSA card devices, from one `pw-dump`) that callers pass down rather than re-fetching; plus `get_volume_mute()`, `set_volume`, `set_mute`, `set_channel_volumes` (raw `channelVolumes` for panning, since PipeWire has no pan control), `set_device_profile`, `load_module` / `unload_module`. |
| `views.py` | ~190 | Pure functions turning the raw node list plus the peer and headset registries into the dict shape that templates and WebSocket broadcasts consume. `build_state()` is the whole-dashboard payload; `peer_view()`, `headset_view()`, `direction_view()` are the pieces. |
| `peers.py` | ~320 | The peer registry. Loads and saves `data/peers.yaml`, seeds the hand-configured peers (`_seed_peers()`: `sagedeck`, `sage-dev` via Roc, `sage` via VBAN), allocates port blocks, writes the Bragi-managed `70-bragi-peers.conf` (`_roc_conf_block()`), and hot-loads a new peer into the live graph (`_hot_load_peer()`). Also the `Peer` and `Ports` dataclasses and `DATA_DIR`. |
| `headsets.py` | ~110 | Auto-detects USB headsets by ALSA card id (whatever is plugged in is the list, no registry), pairs each one's playback and capture nodes, and enables or disables a headset by flipping its ALSA card profile to `off` and back. |
| `ws.py` | ~500 | The WebSocket control plane. `ConnectionManager` (one asyncio queue per tab), `apply_action()` (the verb switch: `set_volume`, `set_balance`, `mute`, `toggle_enabled`, `set_viz_enabled`), `_throttled_apply` (single-flight-per-control worker), `_accept_ts` (client-timestamp ordering), and the broadcasts. Biggest and most fragile file. Read [`realtime-control-plane.md`](realtime-control-plane.md) before changing it. |
| `watcher.py` | ~80 | Runs `pw-mon` and fires a callback on any PipeWire graph change (a headset plugged or unplugged, another client changing a node) so the dashboard reflects changes Bragi did not make. |
| `knob_watcher.py` | ~150 | Reads `/dev/input/eventN` directly for the headset's hardware volume knob, which sends standard `KEY_VOLUMEUP` / `KEY_VOLUMEDOWN` HID events that nothing catches on a headless Pi. Translates knob turns into real volume steps. |
| `audio_state.py` | ~65 | Persists an authoritative `(volume, balance)` pair per pannable node to `data/balance.yaml` and reapplies both together on every change, so the volume fader and the balance pad do not fight each other (both are the same raw `channelVolumes` write underneath). |
| `peer_presence.py` | ~15 | The `/ws/peer/{name}` endpoint. The Bragi Client tray app holds this open for as long as its Roc link is enabled; that live socket is the peer card's real reachability signal, which a loaded Roc module cannot give on its own over UDP. |
| `level_meter.py` | ~260 | Live VU-style metering. Captures each node's signal with `pw-cat --record`, and a single ticker pushes every meter's current level as one `levels` frame per ~50ms rather than one frame per meter. Two gates in `supervise()`: `viz_settings.get_enabled()` (a real off switch, not a display toggle) and `ws.manager.has_clients()`; it reconciles on `ws.manager.wake` as well as its own timer, so opening a tab starts the meters at once. |
| `viz_settings.py` | ~45 | One global on/off toggle for the level meters, persisted to `data/viz_settings.yaml`. Separate from `audio_state.py` because it is global, not per-node, and it gates whether `level_meter.py` captures anything at all. |

## Data files

Everything under `data/` is bind-mounted in production (see
`docker-compose.yml`), so it survives a container recreate or redeploy.

- `peers.yaml` — peers added through the UI. Each entry:

  ```yaml
  - name: sage-laptop
    tailscale_ip: 100.x.x.x
    protocol: roc          # roc | vban
    managed: true          # true = Bragi owns it, removable from the UI
    ports:
      mic_source: 10041
      mic_repair: 10042
      mic_control: 10043
      playback_source: 10051
      playback_repair: 10052
      playback_control: 10053
  ```

  Hand-configured peers (`_seed_peers()`) are `managed: false` and are not
  written here; they live in `~/.config/pipewire/pipewire.conf.d/` on
  `sagepi` directly.

- `balance.yaml` — `{node_name: {volume: float, balance: float}}`,
  balance in `[-1.0, 1.0]`.
- `viz_settings.yaml` — `{enabled: bool}`.

## Deploying a change

Push to `main`. The GitHub webhook triggers a Komodo rebuild and redeploy
on `sagepi` in under a minute. There is no staging environment. See
[`deployment.md`](deployment.md), including why `webhook_force_deploy` has
to be on for this to work at all.

## Style

The modules carry docstrings that explain why they are shaped the way they
are, not just what they do (see `level_meter.py`, `peer_presence.py`,
`audio_state.py`). Match that. A future reader, or a future you, needs the
reasoning more than the summary.
