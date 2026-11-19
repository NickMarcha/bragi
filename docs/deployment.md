# Deployment

Bragi is built for one shape of host: a Linux box (a Raspberry Pi 4 in
practice) running PipeWire as a user session, with one or more USB headsets
plugged in, and peers reachable over Tailscale. Everything runs bare metal
as the PipeWire user: Bragi itself as the `bragi` systemd user service from
a Debian package, next to PipeWire, WirePlumber and, optionally, the
headless Deskflow server.

The Docker setup further down still works, but the package is the way to
run it: Bragi manages host services (Deskflow, the peers, the audio reset)
and devices, which a container can only reach through more and more
mounts.

## Install from the apt repository (Raspberry Pi OS / Debian trixie, arm64)

```bash
curl -fsSL https://nickmarcha.github.io/apt/install.sh | sh
```

That adds the signed repository (<https://nickmarcha.github.io/apt/>, key
fingerprint `753F 00FA D4D7 15C2 CB5B 6FFD EE31 EA61 CD9C F9A5`) and runs
`sudo apt install bragi`. The package puts the app and its own Python
environment in `/opt/bragi`, the user units (`bragi.service`, the peers and
audio-reset units) in `/usr/lib/systemd/user`, and the udev rules in
`/usr/lib/udev/rules.d`, enables the units for every user and starts them
for logged-in ones. Bragi listens on `127.0.0.1:8000`; data stays in
`~/.local/share/bragi/data`, so an install that ran in Docker carries over.

It recommends `deskflow` (the headless Deskflow server, from the same
repository) for the KVM and `bluez` for Bluetooth; without them those
modules are off.

Updating is apt's job: `sudo apt update && sudo apt upgrade`. Moving from
Docker: stop the container (or the Komodo Stack) first, since both use
port 8000, then install the package.

Building it yourself: `packaging/build-deb.sh <version>` in a Debian trixie
container (see `.github/workflows/package.yml`, which does it on every
`bragi-v*` tag and attaches the `.deb` to the release; the apt repository
picks releases up within the hour).

## What the host needs

Settings > Setup checks all of this on the running box and says what's
missing. For most items it has a fix button: starting services runs as
Bragi's user, and the rest goes through the package's root helper
(`bragi-setup`, asked through `/var/lib/bragi/setup-request` like the
updater). The helper only runs its fixed list of fixes, and only for a
user that's running Bragi. Checked: PipeWire and WirePlumber running, the
Roc and VBAN modules, lingering, the `input` group, the HyperX dongle's
usbcore quirk (in `cmdline.txt` on a Pi, a GRUB drop-in elsewhere),
`tailscale serve`, and Bragi's own user services.

- A user with an active PipeWire + WirePlumber session (`systemctl --user
  status pipewire wireplumber`). Enable lingering (`loginctl enable-linger
  <user>`) on a headless box, or the session dies when you log out.
- PipeWire's Roc modules (`libpipewire-module-roc-sink`/`-source`) for Roc
  peers, and `libpipewire-module-vban-send`/`-recv` for VBAN peers. Check
  they're installed with `find /usr/lib -name 'libpipewire-module-roc-*'
  -o -name 'libpipewire-module-vban-*'`.
- Docker with the Compose plugin, only for the Docker setup.
- Tailscale, if peers are elsewhere on your tailnet. Bragi itself doesn't
  care how peers reach it, but the Android app only accepts tailnet
  addresses and HTTPS server URLs.
- For the headset volume knob: read access to `/dev/input`, which the
  compose file grants through the `input` group (see below).
- For the HyperX Cloud III S Wireless battery, mic monitor, auto-off and EQ:
  a kernel quirk and a udev rule. See
  [HyperX Cloud III S Wireless controls](#hyperx-cloud-iii-s-wireless-controls).

## Install with Docker (older setup)

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

To hand them over later, Settings > Setup > Hand-configured peers > Let Bragi
manage them. Bragi looks in `~/.config/pipewire/pipewire.conf.d` for the
files that run those peers' Roc sinks and sources. If every Roc module in a
file belongs to a listed peer, it marks the peers managed, keeping their
ports and node names, so the computers at the other end need no change. It
then writes them into its own config, renames the file to
`<name>.adopted-<date>`, and resets the audio so its config can take over
the ports. A file that also runs anything Bragi doesn't know is left alone.

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
and its settings button opens mic monitoring, the auto-off timer and a 10-band
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

Game controllers paired over Bluetooth need the same for HID passthrough
(`app/gamepad.py`): `host/udev/73-bragi-bluetooth-hidraw.rules` gives every
Bluetooth HID device's hidraw node to the `input` group. Install it the
same way. Without it a controller still works, but only as an Xbox 360
pad, and the log says "no passthrough".

Pairing devices from the dashboard (Bluetooth in the top bar) goes
through the host's BlueZ over the system D-Bus, which `docker-compose.yml`
mounts read-only (`/run/dbus`). BlueZ's default D-Bus policy lets any local
user use it, so nothing else is needed, unless Bluetooth is soft-blocked:
Raspberry Pi OS blocks wireless until it's configured, and the dialog then
says so. Unblock it once with `sudo rfkill unblock bluetooth`; it stays
unblocked across reboots.

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

Mic monitoring in the dialog is Bragi's, not the headset's. The headset's
own monitor (HID op `0x0D`) switches on but plays only noise on the Pi's
headset, so it stays in the dialog as "Headset's built-in monitoring (may
be noisy)". The switch above it runs a `pw-loopback` from the headset's mic
to its own speaker, at 256/48000 (`app/mic_monitor.py`). While it runs, the
whole graph runs at quantum 256 instead of 1024. On the Pi that showed no
new errors on any Roc or VBAN node. The switch and the volume are saved in
`data/mic_monitor.yaml` and come back when Bragi restarts. The volume is the
loopback stream's own, so the computers get the same mic level either way.
Unplugging the dongle stops the loopback, and plugging it back in starts it
again within a couple of seconds.

The built-in monitor and auto-off are read back from the headset after
every change. The EQ can't be read from the headset, so Bragi shows the last EQ
it set (`data/headset_eq.yaml`) and says so in the dialog. Until Bragi has
set one, the sliders sit at 0 and don't reflect what the headset is using.

EQ presets saved from the dialog go to `data/headset_eq_presets.yaml`. The
built-in presets are in `app/eq_presets_builtin.yaml`, and where their
curves come from is in [`eq-presets.md`](eq-presets.md).

### Macro button

The dialog's "Macro button" section gives the headset's single, double and
triple press an action each. The headset tells the gestures apart itself
and sends one report per gesture. Holding the button sends nothing, so
there's no hold action. The wheel belongs to the knob watcher and isn't
offered here.

An action is nothing (the default), Bragi's mic monitoring on/off, or an
HTTP request. A request has a method, a target, extra headers one per line
(`Name: value`) and an optional JSON body, sent as `application/json`
unless the headers say otherwise. The target is a URL, or a unix socket on
the host plus an HTTP path. Use the socket for services that listen only
on the host's 127.0.0.1 or on a socket, since the container can't reach
those over TCP. The container mounts the host's `/run/user/UID`, so a
socket under it works as is.

Actions are saved per dongle in `data/macro_button.yaml` and come back on
restart. Each request gets 3 seconds to answer. A press that arrives while
the same gesture's last request is still running is dropped. Under each
gesture the dialog shows the last result (`HTTP 204 No Content`, `no socket
at ...`, `connection refused`), and Test runs what's in the form once,
saved or not. Results live in memory only. The container log gets a
failure once, then again only when the error changes or the action works.

An example: a double press that sends a keystroke through a web UI that
listens on a socket.

| Field | Value |
|---|---|
| Action | HTTP request |
| Method | POST |
| Send to | a unix socket on the host |
| Socket | `/run/user/1000/deskflow-headless/webui.sock` |
| Path | `/api/keystroke` |
| Headers | `X-Deskflow-Webui: 1` |
| Body | `{"keys": "Control+Shift+Alt+M", "computers": ["sage", "sage-dev", "sagedeck"]}` |

Bragi doesn't know what's behind the socket. Whatever answers there
decides what the request does.

## Moving to a new hub

1. Install Bragi on the new box and work through Settings > Setup.
2. On the old hub, Settings > Backup > Download backup. The file holds
   Bragi's data (peers, layout, headset and mic choices, hotkeys, logins)
   and the KVM's config, including `tls/deskflow.pem`, the certificate the
   Deskflow clients trust. Keep it private: it has the dashboard's logins.
3. On the new hub, Settings > Backup > Restore with that file. What was
   there is kept in `data/backups/pre-restore-*.tar.gz`. Bragi restarts and
   stays **paused**: the peers bridge and the KVM server don't run, even
   after a reboot, so the two hubs never send audio to the same peers.
4. On the old hub, Settings > This hub > Move, with the new hub's address.
   Tray apps 0.1.26+ switch, and the new hub's `hub` message fixes their
   audio address and Deskflow's server address. Older tray apps and phones
   need the new address by hand (phones: the QR code).
5. Stop the old hub, press Take over on the new one, move the USB devices.

## Updating a Docker install

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
