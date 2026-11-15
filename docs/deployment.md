# Deployment

Bragi is built for one shape of host: a Linux box (a Raspberry Pi 4 in
practice) running PipeWire as a user session, with one or more USB headsets
plugged in, and peers reachable over Tailscale. PipeWire, WirePlumber and
the Roc and VBAN modules run bare metal. Only the web UI runs in Docker.

## What the host needs

- A user with an active PipeWire + WirePlumber session (`systemctl --user
  status pipewire wireplumber`). Enable lingering (`loginctl enable-linger
  <user>`) on a headless box, or the session dies when you log out.
- PipeWire's Roc modules (`libpipewire-module-roc-sink`/`-source`) for Roc
  peers, and `libpipewire-module-vban-send`/`-recv` for VBAN peers. Check
  they're installed with `find /usr/lib -name 'libpipewire-module-roc-*'
  -o -name 'libpipewire-module-vban-*'`.
- Docker with the Compose plugin.
- Tailscale, if peers are elsewhere on your tailnet. Bragi itself doesn't
  care how peers reach it, but the Android app only accepts tailnet
  addresses and HTTPS server URLs.
- For the headset volume knob: read access to `/dev/input`, which the
  compose file grants through the `input` group (see below).
- For the HyperX Cloud III S Wireless battery, mic monitor, auto-off and EQ:
  a kernel quirk and a udev rule. See
  [HyperX Cloud III S Wireless controls](#hyperx-cloud-iii-s-wireless-controls).

## Install

Clone the repo on the host, as the PipeWire user, and run the installer:

```bash
git clone https://github.com/NickMarcha/bragi.git
cd bragi
host/systemd/install.sh
```

`install.sh` installs the `systemd --user` units that run Bragi-managed
peers and the "Reset audio" button. It also writes `.env` next to
`docker-compose.yml` with this host's uid, gid and `input` group, so the
container runs as you and can read the headset knob. Run it once; running it
again leaves an existing `.env` alone. `.env.example` lists every value.

Start it:

```bash
docker compose up -d --build
```

The container publishes `127.0.0.1:8000` only. Put something in front of it.

## Exposing it over Tailscale

```bash
sudo tailscale set --operator=$USER   # once, so serve doesn't need sudo
tailscale serve --bg --https=443 http://127.0.0.1:8000
```

That gives you `https://<host>.<tailnet>.ts.net/`, reachable on the tailnet
only, with a certificate Tailscale issues. Use this URL in the Android app
and, as `wss://<host>.<tailnet>.ts.net/ws/peer`, in the desktop client's
`BRAGI_WS_URL`.

Anyone on the tailnet can open the dashboard until you set a password in
its Security dialog. See [`api-and-access.md`](api-and-access.md).

## Host units for managed peers

Peers added from the dashboard don't run inside the container. Bragi writes
them to `~/.local/share/bragi/data/peers.conf` (the data bind mount), a
standalone PipeWire client config, and three `systemd --user` units on the
host run it:

| Unit | Does |
|---|---|
| `bragi-peers.service` | `pipewire -c peers.conf`, a separate process whose nodes join the main daemon, like `filter-chain.service`. `BindsTo=pipewire.service`, so it follows daemon restarts. |
| `bragi-peers.path` | Watches `peers.conf` and starts `bragi-peers-reload.service` when it changes. |
| `bragi-peers-reload.service` | One-shot `systemctl --user restart bragi-peers.service`. A path unit can only *start* a unit, which does nothing to one already running. |

Bragi only writes the file when its content changes, so a redeploy doesn't
restart the peers.

Earlier versions wrote `~/.config/pipewire/pipewire.conf.d/70-bragi-peers.conf`
and hot-loaded peers with a one-shot `pw-cli load-module`, whose modules die
with that process. That file must not come back: the main daemon would load
the same peers as `bragi-peers.service`, on the same ports. `install.sh`
warns if it finds one.

## Peers you already run by hand

A fresh install starts with no peers. If you already have Roc peers in your
own `pipewire.conf.d`, list them in `data/peers.yaml` with `managed: false`
and the node names your config uses. Bragi then shows them and controls
their volume, but never writes their config and won't remove them:

```yaml
- name: laptop
  protocol: roc
  tailscale_ip: 100.64.0.12
  managed: false
  ports: {mic_source: 10021, mic_repair: 10022, mic_control: 10023,
          playback_source: 10031, playback_repair: 10032, playback_control: 10033}
  outgoing_sink_name: laptop-outgoing-sink   # the roc-sink node sending to it
  incoming_source_name: laptop-incoming      # the roc-source node receiving from it
```

Bragi allocates ports for managed peers from 10041 up, leaving 10001-10040
free for hand-configured ones.

## Reset audio button and nightly reset

The dashboard's "Reset audio" button restarts PipeWire, WirePlumber and
`bragi-peers` on the host. It exists because of an incident where every
sender's audio reached the headset as garbled bursts until the stack was
restarted, and the default log level held nothing about why.

The container can't restart host services, so the button only writes
`data/reset-request` (`app/audio_reset.py`). Three more units, installed by
the same `host/systemd/install.sh`, act on it:

| Unit | Does |
|---|---|
| `bragi-audio-reset.path` | Watches `reset-request` and starts `bragi-audio-reset@manual.service`. |
| `bragi-audio-reset-nightly.timer` | Starts `bragi-audio-reset@nightly.service` every hour. The script only resets in the hour picked in the dashboard's Settings (05:00 by default, or off), stored in `data/nightly-reset`. |
| `bragi-audio-reset@.service` | One-shot `~/.local/bin/bragi-audio-reset <reason>` (`host/bragi-audio-reset.sh`). |

The script saves a snapshot first, then restarts. Each snapshot is a folder
`data/incidents/<UTC stamp>-<reason>/` holding pw-top, `wpctl status`,
`pw-dump`, the links, ALSA PCM state, dmesg, the last two hours of the
PipeWire journal, and `levels.txt`: per-second level stats for every stream
feeding a headset, recorded on its own before mixing. A manual reset also
keeps those 5 s recordings (`tap-<node>.wav`, about 1 MB each). A `done`
file marks a finished restart, which is what clears the button's
"Resetting audio…" state. The newest 20 snapshots are kept, at most about
120 MB.

Raspberry Pi OS keeps the journal in RAM (`Storage=volatile`, which spares
the SD card). It survives a PipeWire restart but not a reboot, so the
snapshot's `journal.txt` is the copy that lasts. Reading the journal needs
the `adm` group, which the default Pi user already has.

To reset by hand: `systemctl --user start bragi-audio-reset@manual.service`.

Installs from before the hour became a setting have a timer pinned to 05:00.
Run `host/systemd/install.sh` again to switch to the hourly one.

## HyperX Cloud III S Wireless controls

With a HyperX Cloud III S Wireless dongle (USB `03f0:06be`, or `03f0:02cc`
for the other hardware revision), the headset's card shows the battery,
and its settings button opens mic monitor, the auto-off timer and a 10-band
EQ (`app/headset_hid.py`). Bragi talks to the dongle over its vendor HID
interface. Without one of these dongles none of this appears.

It takes three things on the host.

**1. The usbcore quirk.** Without it the dongle answers nothing on Linux.
At its first enumeration after power-up it checks how much of its
configuration descriptor the host asks for. Windows asks for 255 bytes,
Linux for 9, and after a 9-byte request the dongle stops passing anything
from the headset to the host until it's unplugged. Audio and the volume
knob keep working, so nothing looks wrong, but battery reads come back
`0xFF` or not at all. The quirk flag `r` makes Linux ask for 255. The root
cause is in HyperHeadset issue 36
(https://github.com/LennardKittner/HyperHeadset/issues/36), with the
kernel versions known to support the flag. Older kernels drop flags they
don't know without saying so. Raspberry Pi OS's 6.18.60+rpt has it.

Add it to the kernel command line (`/boot/firmware/cmdline.txt` on a Pi,
one line, space-separated):

```
usbcore.quirks=03f0:06be:r,03f0:02cc:r
```

To apply it without a reboot, then unplug the dongle and plug it back in:

```bash
echo 03f0:06be:r,03f0:02cc:r | sudo tee /sys/module/usbcore/parameters/quirks
```

A dongle that's already stuck stays stuck until it loses power, and a Pi
can keep its USB ports powered through a reboot. Unplug it once with the
quirk active.

**2. Access to the hidraw node.** The node is `root:root 0600` by default.
`host/udev/72-bragi-hyperx-hidraw.rules` gives it to the `input` group,
which the container already joins for the volume knob:

```bash
sudo install -m 644 host/udev/72-bragi-hyperx-hidraw.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=hidraw
ls -l /dev/hidraw*   # the dongle's node: crw-rw---- root input
```

A rule of your own that does the same is fine. Skip this step if you have one.

**3. The compose settings.** `docker-compose.yml` mounts the host's `/dev`
read-only at `/host/dev` and allows the hidraw device major through
`device_cgroup_rules`. It doesn't list the node under `devices:`, because
that copies only the nodes present when the container starts: a replug
gives the dongle a new `hidrawN`, and starting without the dongle plugged
in would fail. The major comes from `HIDRAW_MAJOR` in `.env` (default 241).
`install.sh` writes it to a new `.env`. For an existing one, add it by hand:

```bash
grep hidraw /proc/devices   # e.g. "241 hidraw"
echo HIDRAW_MAJOR=241 >> .env
docker compose up -d
```

If the controls don't appear, the container log says why: `cannot open
/host/dev/hidrawN ... Operation not permitted` is a wrong `HIDRAW_MAJOR`,
`Permission denied` is the udev rule. A dongle that opens but never shows a
battery is the missing quirk.

Mic monitor and auto-off are read back from the headset after every
change. The EQ can't be read from the headset, so Bragi shows the last EQ
it set (`data/headset_eq.yaml`) and says so in the dialog. Until Bragi has
set one, the sliders sit at 0 and don't reflect what the headset is using.

EQ presets saved from the dialog go to `data/headset_eq_presets.yaml`. The
built-in presets are in `app/eq_presets_builtin.yaml`, and where their
curves come from is in [`eq-presets.md`](eq-presets.md).

## Updating

```bash
git pull
docker compose up -d --build
host/systemd/install.sh   # only if host/ changed
```

## Running it as a Komodo Stack

Bragi works as a [Komodo](https://komo.do) git-sourced Stack that builds on
the host. Three settings matter, because Komodo's defaults assume a Stack
pulls a published image:

- `run_build = true`. The default `false` pulls new commits but never
  rebuilds the image, so redeploys silently run old code.
- `auto_pull = false`. The default runs `docker compose pull` first, which
  fails outright for a `build:`-only service:
  `pull access denied for bragi, repository does not exist`.
- `webhook_force_deploy = true`, if you deploy from a GitHub webhook. A
  `/deploy` webhook runs `DeployStackIfChanged`, which compares the
  contents of `docker-compose.yml`, not the commit. A code change never
  touches the compose file, so without this the webhook never rebuilds.

Komodo runs `docker compose` as its Periphery agent's user. If that isn't
the PipeWire user, set `BRAGI_DATA` (and the uid/gid values) in the
Stack's environment, since `${HOME}` resolves to the agent's home.

## Dashboard latency history

The first deployed version took 6.76s per page load. `list_nodes()` was
calling `wpctl get-volume` in a subprocess for every node in the graph (21
on the Pi it ran on), and the route handlers called `list_nodes()` three
times per peer. That was roughly 189 `wpctl` spawns for one page load.

Fixed by fetching the node list once per request and threading it through,
and calling `wpctl get-volume` only for the one to six nodes a request
actually needs. 6.76s to 0.5s, about 13x.

Reading volume straight from `pw-dump`'s `channelVolumes` was considered and
rejected: `wpctl` applies a cube-root scaling curve that the raw value does
not (0.40 displayed is 0.064 raw), so raw reads would drift from what
`wpctl set-volume` writes. The fix kept `wpctl` for reads and just called it
far less often.
