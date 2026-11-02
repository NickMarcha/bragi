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
