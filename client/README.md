# Bragi Client

A Linux tray app (Avalonia, .NET) for desktops that talk to Bragi over Roc.
It turns the Roc link to the Bragi host on and off, shows its status, gives
you a volume window for every peer and headset, and keeps the dashboard's
status dot for this machine live. See
[`../docs/audio-bridge.md`](../docs/audio-bridge.md) for the audio bridge
this is part of.

## Setup

You need PipeWire with the Roc modules (`libpipewire-module-roc-sink.so`
and `-source.so`; on Arch, `pipewire-roc`), Tailscale, and a Bragi server
you can reach.

1. Download `BragiClient.AppImage` from the latest `client-v*` release and
   run it:

   ```bash
   gh release download --repo NickMarcha/bragi --pattern 'BragiClient.AppImage'      --dir ~/.local/bin --clobber
   chmod +x ~/.local/bin/BragiClient.AppImage
   ~/.local/bin/BragiClient.AppImage
   ```

   Keep it somewhere writable and stable. It updates itself in place.

2. On first run the "Connect to Bragi" window opens. Check the four fields
   and press Connect:

   | Field | Prefilled with |
   |---|---|
   | Bragi server | the dashboard's address, e.g. `https://bragi.your-tailnet.ts.net/` |
   | This computer's name in Bragi | the hostname |
   | This computer's Tailscale address | the address on `tailscale0` |
   | API key | only needed if the dashboard has a password (Security > API connections) |

   The client registers with the server, which hands out this peer's ports.
   It then finds the Bragi host's address and the Roc modules, writes
   `~/.config/bragi-client/roc-link.env`, installs and enables
   `bragi-roc-link.service`, and starts it. The peer shows up on the
   dashboard with a live status dot.

3. Turn on "Start at Login" in the tray menu.

Once the link is set up, the tray item reads "Change Bragi connection...".
Use it to change server, name or address; keeping the same name keeps the
same ports, and a peer you already added from the dashboard under the same
name is reused.

If this machine already loads Roc modules from a static `pipewire.conf.d`
file, the window says so. Disable that file first, see
[Migrating from a static drop-in](#migrating-from-a-static-pipewireconfd-drop-in).

### Setting it up by hand

`client/systemd/install.sh` installs the same unit and an example env file
without the tray app. Add the peer in the dashboard, then copy its ports
from `data/peers.yaml` on the server into `~/.config/bragi-client/roc-link.env`
([`systemd/roc-link.env.example`](systemd/roc-link.env.example) explains
each value) and run `systemctl --user enable --now bragi-roc-link.service`.

## Components

- `src/Bragi.Client/` is the Avalonia tray app. `Config/LinkSetup.cs` and
  `Tray/LinkInstaller.cs` are "Connect to Bragi...".
- `systemd/` is the `systemd --user` unit and wrapper script that own the
  Roc link. The app ships a copy and installs it; `install.sh` does the
  same by hand. Day to day the app only calls `systemctl --user
  start/stop/is-active` on the unit and never talks to PipeWire directly.
- `roc/` builds the patched libroc that releases bundle (see below).

## Volume and web UI

The tray has two direct actions: `Volume` and `Open web UI`.

`Volume` opens one resizable window containing every peer and headset,
laid out like the dashboard's console: the same colours, a status dot and
name per device, and two vertical faders with the volume on the thumb and
a mute button under each. Volumes run from 0% to 150%. This device comes
first, then the dashboard's device order. Devices with nothing to mix
(a switched-off headset, a peer whose client is away or whose link is
down) fold to their header in a stack at the end. Devices wrap as the
window resizes, and the faders share the window's height between rows.
Left-clicking the tray icon opens the same window, and clicking either
again brings it forward. The look is in
`src/Bragi.Client/Volume/VolumeStyles.axaml`; keep its colours in step
with `app/static/style.css`.

Strips use the dashboard's names. A peer has Mic (the headset microphone
received by that computer) and Audio (sound sent from that computer to the
headset); a phone shows Headset and Audio, or Mic in microphone mode. A
headset has Speakers and Mic. These are Bragi link volumes.

The window identifies this device with `PEER_NAME` and derives the web UI
and control URLs from `BRAGI_WS_URL`, such as
`wss://bragi.example.ts.net/ws/peer`. Missing or invalid server settings
disable the two tray actions. Without a peer name, all devices still appear.
If the web UI has a password, set `BRAGI_API_KEY` too.

The window loads the device list when opened and uses one dashboard
WebSocket for live changes, reconnecting after a network interruption.
Unavailable directions and disconnected controls are disabled. Closing the
window closes that connection. An idle tray has no volume connection.

## Dashboard presence heartbeat

`Tray/PeerPresenceClient.cs` holds a WebSocket open to the server's
`/ws/peer/{PEER_NAME}` (`app/main.py`, `app/peer_presence.py`) for as long
as the link is enabled. It reconnects with exponential backoff (2s up to
30s) and closes cleanly when the link is turned off. This drives the
dashboard's status dot for this peer. Roc runs over connectionless UDP, so
a PipeWire node existing on the server says nothing about whether this
machine is there. Missing `PEER_NAME` or `BRAGI_WS_URL` turns the heartbeat
off without affecting anything else.

## Client tests

```bash
dotnet test client/Bragi.Client.slnx -c Release
```

Run from the repository root with the .NET 10 SDK. The tests use Avalonia's
headless UI and a local WebSocket server to check direction mapping,
slider and mute actions, stale updates, reconnects, offline controls,
the shared device window, and configured-server URLs. They do not require PipeWire
or a desktop session. Run them before releasing client changes.

Desktop-shell tray rendering, the left click on the tray icon and opening
the user's browser still need a
manual check on the installed client.

## Why the systemd unit isn't a `oneshot`

The obvious design is a `Type=oneshot` unit whose `ExecStart` runs
`pw-cli load-module ...` and whose `ExecStop` runs `pw-cli destroy ...`. It
does not work. A module loaded with `pw-cli load-module <name> <args>` lives
in that pw-cli process's own connection, not in the PipeWire daemon. The
node is destroyed the instant pw-cli exits, which for a one-shot call is
right after the command returns. It looks like it worked: no error, and a
module id is printed. Then the node silently vanishes.

So `bragi-roc-link.service` is `Type=notify`, and its `ExecStart`
(`bragi-roc-link-run.sh`) *is* the long-lived pw-cli session. It opens a
FIFO, starts `pw-cli` reading from it in the background, sends the two
`load-module` commands, keeps the FIFO's write end open on a spare file
descriptor, and waits on pw-cli. Because the write end never closes,
pw-cli's stdin never sees EOF and it never exits on its own. `systemctl
stop` sends SIGTERM to the unit's cgroup, pw-cli disconnects, and PipeWire
cleans up the nodes. No `pw-cli destroy`, no module-id state file.

Two bugs came out of testing the real `systemctl --user start/stop` cycle:

1. pw-cli prints harmless `Error: "unsupported type
   PipeWire:Interface:SecurityContext"` lines during its startup registry
   sync. An early readiness check matched any `^Error:` line and killed a
   working link. It now matches `Could not load module`, pw-cli's real load
   failure message.
2. `is-active` said `failed` instead of `inactive` after a normal stop.
   pw-cli exits nonzero when SIGTERM'd, and `set -e` passed that through as
   the script's exit code before its "was this a deliberate stop?" check
   ran. The trailing `wait` now runs under `set +e`, and the script exits 0
   when its own SIGTERM trap fired.

## Migrating from a static `pipewire.conf.d` drop-in

If this machine already loads Roc modules from a static config (say
`~/.config/pipewire/pipewire.conf.d/99-roc-bragi.conf`, read only at
`pipewire.service` startup), disable it first. Otherwise the static config
and this unit load the same modules on the same ports:

```bash
mv ~/.config/pipewire/pipewire.conf.d/99-roc-*.conf{,.disabled}
systemctl --user restart pipewire.service   # brief audio drop, do this once
pw-dump | grep -i roc                        # confirm empty before enabling the unit
```

WirePlumber remembers the selected default sink and source by node name
(`default.configured.audio.sink`/`.source`). If `LOCAL_SINK_NAME` and
`LOCAL_SOURCE_NAME` match the names the static config used, it re-selects
them once the unit starts, with no manual re-routing.

## Patched libroc (installed by the tray app)

The distro roc-toolkit 0.4.0 hangs the link after about 24.9 hours of
streaming. `LatencyTuner::report_()` compares uint32 sample positions with a
plain `>=`, and once its position wraps past 2^32 the loop never exits,
pinning PipeWire's `data-loop.0` at 100%. Upstream master still has the bug.

Releases bundle a fixed build (`client/roc/build-libroc.sh` plus
`client/roc/patches/`). On startup the tray app copies it to
`~/.local/lib/bragi/roc/libroc.so.0.4` and writes
`~/.config/systemd/user/bragi-roc-link.service.d/50-bragi-libroc.conf`,
which sets `LD_LIBRARY_PATH` for the link unit only. If either file changed
and the link is running, the app restarts the link once. Nothing to do by
hand. To check it took:

```bash
grep -o '/[^ ]*libroc[^ ]*' /proc/$(pgrep -x pw-cli)/maps | sort -u
# expect ~/.local/lib/bragi/roc/libroc.so.0.4, not /usr/lib/libroc.so.0.4
```

To back it out, delete the drop-in, `systemctl --user daemon-reload`, and
restart the unit.

## Releases and self-update (Velopack)

`Program.cs` runs `VelopackApp.Build().Run()` before anything else. The tray
app checks for updates silently on startup and on demand from "Check for
Updates" (`Tray/UpdateService.cs`), against the GitHub Releases of the repo
it was built from, on the `linux` channel. The repo is baked in at build
time from the csproj's `BragiRepoUrl` property; the release workflow sets it
to the repo it runs in, so a fork's builds update from the fork.

Only a Velopack-installed build checks for updates. A plain `dotnet build`
reports `IsInstalled == false` and both checks do nothing. On first run the
AppImage logs to `/tmp/velopack_BragiClient.log`; `Retrieving latest
release feed` there means the check is running.

To cut a release:

1. Bump `<Version>` in `client/src/Bragi.Client/Bragi.Client.csproj`.
2. Tag that commit `client-v<version>` and push the tag.
   `.github/workflows/client-release.yml` runs on `client-v*.*.*` tags
   only, since `main` also carries server-only commits. It builds the
   patched libroc, packs with `vpk`, and publishes a GitHub Release with
   `BragiClient.AppImage`, the `.nupkg` and `releases.linux.json`.
