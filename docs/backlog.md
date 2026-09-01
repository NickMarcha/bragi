# Backlog

Known bugs and deferred work. Most of this came out of deck-assistant issue
#061. Items are roughly ordered by how much they bite.

## Bugs

### Re-enabling a headset can leave the card on with no nodes

Seen once in roughly six enable/disable cycles on `sagepi`'s HyperX Cloud
III S Wireless (device 88), while verifying the enable/disable latency fix.

After a disable, the following enable wrote the card's restore profile back
(`active_profile_index` went to 1, which is what it was before), the UI
correctly showed the headset enabled - and PipeWire never created the
card's Sink/Source nodes. So the card claims a working profile while
`list_headsets()` reports `playback=None capture=None`, both strips read
offline, and no audio path exists.

It is a session-manager wedge, not hardware and not the ALSA layer:

- `lsusb` and `/proc/asound/cards` both still showed the card.
- `/proc/asound/card1/pcm0{p,c}/sub0/status` both read `closed`, so nothing
  held the PCM open and nothing had failed to open it.
- `journalctl --user -u wireplumber -u pipewire` logged nothing at all.
- Writing profile 0 (off) again did *not* stick - it read back as 1 within
  half a second, which is the tell that WirePlumber's own restore policy
  was fighting the write.

**Recovery that works, without restarting anything:** select a *different*
real profile, then go back. On device 88, `wpctl set-profile 88 2`
(`output:analog-stereo`) created the sink within one second, after which
`wpctl set-profile 88 1` restored both playback and capture immediately.
Re-selecting the profile the device already believes is active is what does
nothing.

Not fixed, deliberately. A "verify the nodes appeared, else nudge through
another profile" retry loop is speculative complexity for something seen
once and not reproducible on demand, and it would put a second `pw-dump`
plus a wait back on the click path that was just brought from 9.7s to
0.55s. Worth revisiting if it turns out to be common - the recovery above
is a one-liner in the meantime.

### UI-added peers never reach the live PipeWire graph

`app/pipewire.py:189` `load_module()` runs `pw-cli load-module` as a
one-shot `subprocess`. A module loaded that way lives inside that pw-cli
process's connection to the daemon and is destroyed the instant the process
exits. No error is reported.

So `_hot_load_peer()` in `app/peers.py` "succeeds" (a module id comes back,
`peers.yaml` and the generated `70-bragi-peers.conf` are both written
correctly), but nothing exists in the live graph until the next
`systemctl --user restart pipewire.service` on `sagepi` picks up the config
file the normal way.

This is the same trap the client-side systemd unit already works around with
a long-lived pw-cli session (see
[`audio-bridge.md`](audio-bridge.md#the-client-side-systemd-unit) and
`client/README.md`). The server needs the same treatment: hold one pw-cli
session open for the lifetime of the process, or have the add-peer flow
trigger its own `pipewire.service` restart.

Found while testing an Android peer (deck-assistant #061, "Android Peer
Attempt"). Not yet fixed.

### UI-added peers are misclassified and never auto-link to the headset

`app/peers.py` sets `media.class = "Audio/Source"` on the `roc-source`
module for UI-added peers, in two places: `_roc_conf_block()` at line 171
(the config-file template) and `_hot_load_peer()` at line 288 (the hot-load
dict).

`pw-dump` confirms the effect: a hand-configured working peer is
`Stream/Output/Audio`; a UI-added one is `Audio/Source`. WirePlumber's
default-sink auto-link policy only fires for `Stream/Output/Audio`. An
`Audio/Source` node is treated like a capture device, which nothing routes
anywhere, so the peer's incoming audio never reaches the headset even when
the network path is fine.

Fix: delete the `media.class` override in both spots so the module keeps its
real default (`Stream/Output/Audio`), matching the hand-configured peers
that already work.

Even with the ports right, this bug alone produces total silence on a
UI-added peer. Found in the same #061 Android investigation. Not yet fixed.

### Removing a session-added peer after a Bragi restart does not hot-unload it

The persisted config file is rewritten without the peer, but the live
PipeWire modules linger until the next `pipewire.service` restart, because
the module ids needed to unload them only exist in Bragi's process memory,
not on disk. Already listed in the README's known limitations.

## Operational

### vban-sage.service should not need a manual restart

A `pipewire.service` restart on `sagepi` silently breaks the running
`vban_receptor` / `vban_emitter` processes, which hold a dead PulseAudio
socket open and never reconnect. The current fix is a manual
`systemctl --user restart vban-sage.service`. It belongs in the unit as a
`BindsTo=pipewire-pulse.service` dependency, or as a reconnect-with-backoff
loop around the `pa_simple_write` failure. See
[`audio-bridge.md`](audio-bridge.md#restart-coupling-vban-does-not-self-heal).

### Autostart entry for the tray app on sage-dev

`sagedeck` has `~/.config/autostart/bragi-client.desktop`. `sage-dev` has
the file written but the tray GUI has not been launched there since (X11,
and a non-interactive SSH shell has no `DISPLAY` access). Someone needs to
start it locally on `sage-dev` once, or log out and back in.

## Deferred features

### VBAN peers cannot be added or removed from the UI

`vban_emitter` / `vban_receptor` always register as a PipeWire client
literally named `vban`. Bragi tells the two directions apart by stream class,
which only works with a single VBAN peer. A second one needs a different
disambiguation strategy, probably tracking VBAN peers by PID or wrapping
each in its own differently-named systemd service.

### Hand-configured peers cannot be removed from the UI

Peers seeded in `app/peers.py`'s `_seed_peers()` (`sagedeck`, `sage-dev`,
`sage`) are volume-controllable but not removable. Editing them means
touching `~/.config/pipewire/pipewire.conf.d/` on `sagepi` directly.

### Dual-headset playback on sagepi

Deprioritized in favor of one headset working reliably. The Pi 4's shared
Full-Speed USB hub cannot carry two headset streams stably even forced to
16-bit. Fixing it needs a udev or path-unit auto-relink-on-reconnect script,
or accepting single-headset as the design. See
[`audio-bridge.md`](audio-bridge.md#dual-headset-playback-and-why-sagepi-runs-one-headset).

### Second headset mic capture

Only the first headset's mic feeds the remote `roc-sink`s. If dual-headset
comes back, the second headset's mic capture side still needs wiring.

### Android peer (Roc Droid)

Parked. Roc Droid hardcodes ports 10001/10002 and the RS8M FEC protocol in
the app (`SenderReceiverService.kt`), with no editable port fields and no
plain-RTP option. Every other peer here runs `fec.code = disable`, which
expects plain RTP. It also connects no control/RTCP endpoint, while Bragi's
model assumes a six-port block per Roc peer. Interop needs the two bugs
above fixed plus `fairphone` hand-configured on `sagepi` like the `sage`
VBAN peer (`local.source.port = 10001`, `local.repair.port = 10002`,
`fec.code = "rs8m"`, no control port, no `media.class` override).

### Windows tray client

`sage`'s VBAN link has no enable/disable/status control outside the web UI.
The Avalonia tray client is Linux-only right now.

### Realtime control plane

Still under live observation. See
[`realtime-control-plane.md`](realtime-control-plane.md). Several rounds of
"looks fixed" in one session turned out not to be under real drag timing.
Worth staying skeptical until it has held up under more than one session of
real use.
