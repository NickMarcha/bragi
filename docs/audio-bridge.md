# The audio bridge

`sagepi` is a Raspberry Pi 4 with a USB headset plugged into it. It acts as
an always-on hub for that headset: it captures the mic and sends it to
`sage`, `sage-dev`, and `sagedeck` over Tailscale, and it mixes the audio
coming back from those machines onto the headset speakers. The Pi is the hub
on purpose. None of the desktops need to be powered on for audio to move
between another device and the headset.

Bragi is the control panel for this. It does not carry audio itself. It
loads and unloads PipeWire modules and sets volumes. This document is the
layer underneath Bragi: the PipeWire graph, the two network protocols, and
the parts that took several tries to make stable.

The original build log is deck-assistant issue #061. This doc is the
distilled version that lives with the code.

## Two protocols, on purpose

Roc for the Linux peers, VBAN for the Windows one. Both run on `sagepi` at
the same time and feed the same headset capture and playback nodes.

| Protocol | Used for | Why |
|---|---|---|
| Roc, via PipeWire `module-roc-sink` / `module-roc-source` | `sage-dev`, `sagedeck` | Built for real-time network audio. Has FEC with a configurable repair-packet ratio, which addresses the bursty-delivery underruns that VBAN hit in the older deck-assistant #008 setup. Ships as PipeWire modules, so there is no separate daemon and no boot-order retry wrapper. Upstream supports Linux, macOS, and Android only. |
| VBAN, via `vban_emitter` / `vban_receptor` built from source | `sage` (Windows) | Roc has no Windows client. `sage` already ran Voicemeeter Banana from deck-assistant #008/#014. VBAN has no FEC, and its PulseAudio backend drops buffer-latency settings on the capture side, so the Pi's emitter needs `PULSE_LATENCY_MSEC=5`. Voicemeeter's own VBAN implementation is solid, so only the Pi side carries risk and that part is already worked out. |

Plain PipeWire RTP was rejected as not production-ready. NDI was rejected as
video-first, proprietary, heavier at runtime, and no FEC advantage over Roc.
Snapcast was rejected as the transport because it is playback-only with no
mic uplink, but its web UI was a useful reference for this one.

Running both protocols is a real cost of supporting Windows, not a
preference.

## Port allocation

Each peer gets its own block of UDP ports so `sagepi` can talk to several
devices at once with no collisions. Roc uses three ports per direction:
source, repair, control.

| Peer | Direction | Source | Repair | Control |
|---|---|---|---|---|
| `sagedeck` | mic, `sagepi` to `sagedeck` | 10001 | 10002 | 10003 |
| `sagedeck` | playback, `sagedeck` to `sagepi` | 10011 | 10012 | 10013 |
| `sage-dev` | mic | 10021 | 10022 | 10023 |
| `sage-dev` | playback | 10031 | 10032 | 10033 |

VBAN multiplexes by stream name rather than by port, so `sage` uses one port
(6980) for both directions.

`fec.code` is `disable` on every Roc peer. FEC tuning was deferred and never
turned out to be needed for casual voice use.

## Building the Roc modules on the CachyOS clients

`sage-dev` and `sagedeck` both hit this. CachyOS ships its own PipeWire
rebuild (`1:1.6.8-1.2`, znver4-optimized) that Arch's `pipewire-roc` package
will not install against. Downgrading PipeWire to satisfy it would drag
`kpipewire` out with it and break Plasma's screen-share. So the fix is to
build only the two module `.so` files from source and leave the system
package untouched.

```bash
git clone --branch <exact-installed-pipewire-version-tag> --depth 1 \
  https://gitlab.freedesktop.org/pipewire/pipewire.git src
cd src
meson setup build -Dauto_features=disabled -Droc=enabled \
  -Dpipewire-alsa=disabled -Dpipewire-jack=disabled -Dpipewire-v4l2=disabled \
  -Dlibcamera=disabled -Dsession-managers='[]' -Dexamples=disabled \
  -Dtests=disabled -Ddocs=disabled -Dman=disabled -Dlibsystemd=disabled \
  -Dudev=disabled --buildtype=release
# do NOT disable spa-plugins, it breaks the modules/meson.build dependency graph
ninja -C build src/modules/libpipewire-module-roc-sink.so \
                src/modules/libpipewire-module-roc-source.so
```

Copy the two `.so` files somewhere persistent. The build needs
`libroc-dev`'s `roc.pc` pkg-config file (from the `roc-toolkit` package) and
PipeWire's own dev headers.

The built module binds to whatever `libpipewire-0.3.so.0` is already loaded
in the daemon process, not the copy in the build tree, because ld.so reuses
an already-resident soname before it consults RPATH. So loading it into a
live session is safe. This was confirmed on `sagedeck`'s running desktop
with no disruption, including across an accidental reboot mid-session.

For the daemon to find the module, `PIPEWIRE_MODULE_DIR` has to be set on
the process that resolves the module name:

- For config-file modules (`context.modules` in `pipewire.conf.d/*.conf`),
  set it on the `pipewire.service` unit through a user drop-in
  (`~/.config/systemd/user/pipewire.service.d/*.conf`,
  `Environment=PIPEWIRE_MODULE_DIR=...`). This is the one that matters in
  production.
- `pw-cli load-module` reads `PIPEWIRE_MODULE_DIR` from pw-cli's own shell
  environment, not just the daemon's.
- Passing a full path as the module name does not work. `pw-cli` only does
  `<search-dir>/<name>.so` lookups.

## The client-side systemd unit

`sage-dev` and `sagedeck` each run `bragi-roc-link.service` (in
`client/systemd/`), which owns that machine's Roc link. The Bragi Client
tray app only ever calls `systemctl --user start/stop/is-active` on it.

The unit is `Type=notify`, not `oneshot`, and that choice is load-bearing. A
module loaded by `pw-cli load-module` lives inside that one pw-cli process's
connection to the daemon. When the process exits, the module and its node
disappear, with no error printed. A oneshot unit whose `ExecStart` runs
`pw-cli load-module` would load the link and then drop it the moment the
command returned.

So `bragi-roc-link-run.sh` holds one long-lived `pw-cli` session open. It
opens a FIFO, starts `pw-cli` reading from it, sends the `load-module`
commands, keeps the FIFO's write end open with a spare file descriptor so
pw-cli's stdin never sees EOF, then waits on the process. `systemctl stop`
sends SIGTERM to the cgroup, pw-cli disconnects, and PipeWire cleans up the
nodes on its own. No `pw-cli destroy` call and no module-id state file.

The same trap exists in Bragi's own server code. See
[`backlog.md`](backlog.md).

## Declarative config on sagepi

Nearly every recurring bug in early testing traced back to manual `pw-link`
state or a `wpctl set-default` choice not surviving a `pipewire.service`
restart. The fix was to make all of it declarative.

- The four `roc-sink` / `roc-source` module entries plus two
  `module-loopback` instances (one per Roc peer, for mic fan-out) live in
  one file, `~/.config/pipewire/pipewire.conf.d/60-roc-bridge.conf`. Load
  order within the array matters: each `module-loopback` must come after the
  `roc-sink` it names in `playback.props.target.object`.
- The playback direction self-heals for free. `roc-source` nodes are
  `Stream/Output/Audio` class, so WirePlumber's default-sink policy
  auto-links them to the headset for as long as the headset is the default
  sink. Set that once with `wpctl set-default` and WirePlumber stores it in
  `~/.local/state/wireplumber/`.
- The mic direction needs the `module-loopback` instances because a raw
  ALSA capture port does not want to route anywhere on its own. Each
  loopback pins `capture.props.target.object` to the headset mic by name and
  `playback.props.target.object` to the matching `roc-sink` by name.

Verified by restarting `pipewire.service` twice in a row (all 22 links
re-established both times) and by a real `sudo reboot` on `sagepi`
(`tailscaled` and `pipewire` started in the same second, no boot-order race,
no manual repair). The #008-era `wait_for_network` dependency that VBAN's
CLI tools needed does not apply to Roc's UDP-socket modules.

## Restart coupling: VBAN does not self-heal

The Roc legs recover from a `pipewire.service` restart by themselves,
because the daemon reloads their `context.modules` entries. VBAN does not.

`vban_receptor` and `vban_emitter` connect to their playback target through
the PulseAudio compatibility socket (`-b pulseaudio`). When
`pipewire.service` restarts, `pipewire-pulse` restarts with it, and the
long-lived VBAN processes keep their now-dead `pa_simple` connection open
and error on every write instead of reconnecting. `vban_receptor` has no
reconnect logic for a severed Pulse socket.

So any `pipewire.service` restart on `sagepi` needs a follow-up:

```bash
systemctl --user restart vban-sage.service
```

This belongs in the unit as a `BindsTo=pipewire-pulse.service` dependency,
or as a reconnect-with-backoff loop around the `pa_simple_write` failure.
See [`backlog.md`](backlog.md).

## Dual-headset playback, and why sagepi runs one headset

The goal was two physical headsets on `sagepi` playing the same mixed feed.
It works briefly, then breaks on the next reconnect event.

The Pi 4 has one shared Full-Speed USB 2.0 hub behind all four ports,
including the blue USB 3.0 ones. Moving a headset dongle to a different port
does not give it a separate bandwidth budget, because these are USB Audio
Class devices running at Full Speed (12M), not real SuperSpeed devices. Two
24-bit headset streams do not fit:

```
usb 1-1.4: Not enough bandwidth for altsetting 2
```

Forcing both devices from 24-bit down to 16-bit makes them fit, and both
play at once. The rule is a WirePlumber ALSA monitor entry setting
`audio.format = "S16LE"`, which drops the packet size from 432 to 288 bytes.
But at 16-bit the two streams are still right at the edge, and any
renegotiation event (a peer reconnecting, a client restarting its own
`pipewire.service`) knocks one headset back to `Stop`/`Stop` and needs a
manual relink.

So `sagepi` runs one headset, the HyperX Cloud III S Wireless. The second is
unlinked. Fixing this properly needs either a udev or path-unit
auto-relink-on-reconnect script, or a decision that single-headset is the
design. See [`backlog.md`](backlog.md).

## Gotchas worth knowing before you debug

- A `roc-source` node has `media.class = "Stream/Output/Audio"`, not
  `Audio/Source`, even though it is conceptually a source of received audio.
  `wpctl status` lists it under Streams, never under Sinks or Sources.
- `audio.position = [ MONO ]` in a `roc-source` or `roc-sink` config is
  ignored on the installed PipeWire version. The nodes get stereo `FL`/`FR`
  ports regardless. Link the mono source to both `FL` and `FR` sink ports
  instead.
- `wpctl` applies a cube-root scaling curve that `pw-dump`'s raw
  `channelVolumes` does not. 0.40 as shown by `wpctl` is 0.064 raw. Read
  volume with `wpctl get-volume`, not from `pw-dump`, or the number drifts
  from what `wpctl set-volume` writes.
- Apps cache their audio device at stream-open time. Changing the system
  default with `wpctl set-default` does not migrate an already-running
  stream. Firefox and Discord both need a restart after a default change.
- The HyperX Cloud III Wireless earcup dial adjusts the software volume
  slider, not a separate hardware attenuation stage, so there is nothing
  extra to rule out when debugging no sound on that headset. It also has
  hardware sidetone (the dongle mixes the mic into your own ear locally),
  adjustable only through HyperX's Windows-only software, so it stays on
  here.

## References

- PipeWire ROC sink module: https://docs.pipewire.org/page_module_roc_sink.html
- PipeWire ROC source module: https://docs.pipewire.org/page_module_roc_source.html
- Roc Toolkit FEC internals: https://roc-streaming.org/toolkit/docs/internals/fec.html
- deck-assistant #061, the full build log this doc is distilled from.
- deck-assistant #008 (archived), the earlier point-to-point VBAN setup and its gotchas.
