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

## Operational

### Retire vban-sage.service on sagepi

VBAN peers now run from `peers.conf`, so the old `vban_emitter`/`vban_receptor`
unit must be disabled once the change is deployed. Steps in
[`audio-bridge.md`](audio-bridge.md#switching-sagepi-over). After that, a
`pipewire.service` restart no longer needs a manual VBAN restart. The
`~/.local/bin/vban_*` binaries can be removed once the new path has held up.

### Autostart entry for the tray app on sage-dev

`sagedeck` has `~/.config/autostart/bragi-client.desktop`. `sage-dev` has
the file written but the tray GUI has not been launched there since (X11,
and a non-interactive SSH shell has no `DISPLAY` access). Someone needs to
start it locally on `sage-dev` once, or log out and back in.

## Deferred features

### Hand-configured peers cannot be removed from the UI

Roc peers seeded in `app/peers.py`'s `_seed_peers()` (`sagedeck`, `sage-dev`)
are volume-controllable but not removable. Editing them means
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

### Android app

Roc Droid was a dead end (hardcoded ports and FEC, no control endpoint), so
Bragi has its own native Kotlin app under `android/`, released as signed APKs
on GitHub with in-app update checks. On the FP5 the user has confirmed
microphone and device-audio sending, listening to the headset mix, a clean
Stop, and, in 0.1.9, about 100 ms of phone-side listening delay with no
underruns. Listen-only mode (0.1.8) is under test on the phone. Current state
and the device checks still outstanding are in
[`../android/README.md`](../android/README.md).

Still open:

- **Sending delay.** Microphone and device-audio capture still use Android's
  default recording buffers; listening got the low-latency treatment in 0.1.9,
  sending has not.
- **Roc jitter headroom.** Listening keeps a fixed 40 ms on top of the playback
  buffer. Lowering it needs a jitter measurement over Tailscale, or a setting.
- **Pi-side delay** of the managed loopback and Roc sender is unmeasured.
- **Listening delay in the web UI.** The app shows it; the peer card does not.
- **Stale saved Tailscale IP.** A saved address overrides fresh detection. This
  caused the first listening failure (issue #1) when the phone's address changed.
- **Listen-only status line** reads "Sender paused · Listening".
- **16 KiB-page phones.** The bundled Roc native libraries use 4 KiB alignment.
- **F-Droid**: needs a source-built Roc AAR and reproducible APK builds.
- **Pi mic as an Android microphone** for other apps: research only.

### Windows tray client

`sage`'s VBAN link has no enable/disable/status control outside the web UI.
The Avalonia tray client is Linux-only right now.

### Realtime control plane

Still under live observation. See
[`realtime-control-plane.md`](realtime-control-plane.md). Several rounds of
"looks fixed" in one session turned out not to be under real drag timing.
Worth staying skeptical until it has held up under more than one session of
real use.
