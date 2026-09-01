# Bragi Client

A tray/taskbar app (Avalonia UI, .NET) for `sage-dev` and `sagedeck` that
turns the Roc audio link to `sagepi` on/off and shows its status, without
needing to open the Bragi web UI or touch PipeWire directly. See
[`../docs/audio-bridge.md`](../docs/audio-bridge.md) for the network audio
bridge this is part of.

## Components

- `src/Bragi.Client/` — the Avalonia tray app itself.
- `systemd/` — the systemd `--user` unit + wrapper script that actually
  owns the Roc link, installed once by hand per machine (`install.sh`).
  The tray app only ever calls `systemctl --user start/stop/is-active` on
  this unit — it never talks to PipeWire directly.

## Why the systemd unit isn't a `oneshot`

The obvious design — a `Type=oneshot` unit whose `ExecStart` runs
`pw-cli load-module ...` and whose `ExecStop` runs `pw-cli destroy ...` —
**does not work**, confirmed by direct testing on `sage-dev` while building
this: a module loaded via `pw-cli load-module <name> <args>` is loaded into
**that pw-cli process's own connection**, not into the PipeWire daemon. The
resulting node is destroyed the instant that pw-cli process exits — which,
for a one-shot invocation, is immediately after the command returns. This
is true even though it *looks* like it worked (no error, a module id is
printed) — the node briefly exists and then silently vanishes.

(The Bragi server's own `app/pipewire.py::load_module`/`peers.py` use this
exact same one-shot `pw-cli load-module` pattern for hot-loading peers, and
it has the same latent bug there. Tracked in
[`../docs/backlog.md`](../docs/backlog.md).)

**The fix**: `bragi-roc-link.service` is `Type=notify`, and its
`ExecStart` (`bragi-roc-link-run.sh`) *is* the long-lived pw-cli session:
it opens a FIFO, starts `pw-cli` reading from it in the background, sends
the two `load-module` commands, keeps the FIFO's write end open (via an
open file descriptor) for as long as the script runs, and then just waits
on the pw-cli process. Because the write end is never closed, pw-cli's
stdin never sees EOF, so it never exits on its own — the modules (and
their nodes) stay loaded for exactly as long as this process does.
`systemctl stop` sends SIGTERM to the whole unit's cgroup, which kills
pw-cli, which disconnects, which makes PipeWire clean up the nodes
automatically — no explicit `pw-cli destroy` or module-id state file
needed at all, which is simpler than the original design.

Two bugs found and fixed while confirming this end-to-end (both from
direct `systemctl --user start/stop` testing, not just code review):

1. **False-positive failure detection**: pw-cli prints benign
   `Error: "unsupported type PipeWire:Interface:SecurityContext"` /
   `...Profiler` lines during its own startup registry sync. An early
   version of the readiness check matched any `^Error:` line and killed an
   otherwise-working link. Fixed to match `Could not load module`
   specifically (pw-cli's actual load failure message).
2. **`is-active` reporting `failed` instead of `inactive` after a normal
   stop**: pw-cli exits with a nonzero (killed-by-signal) status when
   SIGTERM'd, and `set -e` was letting that propagate as the wrapper
   script's own exit code before the script's own "was this a deliberate
   stop?" check ever ran. Fixed by wrapping the trailing `wait` in
   `set +e`/`set -e` and always exiting 0 when the script's own SIGTERM
   trap fired.

Both were caught by testing the actual `systemctl --user start/stop`
cycle against real `pw-dump` output, not by reasoning about the script.

## Per-machine setup

```
cd client/systemd
./install.sh
# edit ~/.config/bragi-client/roc-link.env for THIS machine (see
# roc-link.env.example's comments - sage-dev's real values are filled in
# as the example; sagedeck's are NOT confirmed, verify live first)
systemctl --user enable --now bragi-roc-link.service
systemctl --user status bragi-roc-link.service
```

### Migrating from a static `pipewire.conf.d` drop-in

If this machine already has a static Roc config (e.g. sage-dev had
`~/.config/pipewire/pipewire.conf.d/99-roc-sagepi.conf`, loaded only at
`pipewire.service --user` startup), it must be disabled first, or the
static config and this unit will both load the same modules on the same
ports:

```
mv ~/.config/pipewire/pipewire.conf.d/99-roc-*.conf{,.disabled}
systemctl --user restart pipewire.service   # brief audio drop, do this once
pw-dump | grep -i roc                        # confirm empty before enabling the unit
```

WirePlumber remembers the previously-selected default sink/source by node
name (`default.configured.audio.sink`/`.source`), so as long as the new
unit's `LOCAL_SINK_NAME`/`LOCAL_SOURCE_NAME` match what the static config
used, it re-selects them automatically once the unit starts - no manual
re-routing needed. Confirmed on sage-dev's migration (2026-08-13).

## Self-update (Velopack)

`Program.cs` runs `VelopackApp.Build().Run()` before anything else, and the
tray app checks for updates silently on startup plus on-demand via the
"Check for Updates" menu item (`Tray/UpdateService.cs`), both against this
repo's GitHub Releases on the `linux` channel. Releases are cut by:

1. Bump `<Version>` in `client/src/Bragi.Client/Bragi.Client.csproj`.
2. Tag that commit `client-v<version>` (e.g. `client-v0.1.0`) and push the
   tag - `.github/workflows/client-release.yml` triggers on
   `client-v*.*.*` tags specifically (not push-to-main, since this repo's
   `main` also carries server-only commits that shouldn't fire a client
   release), builds, packs via `vpk`, and publishes a GitHub Release.

`client-v0.1.0` was cut on 2026-09-01 and the workflow ran clean end to end
in 42s: `vpk pack` built `BragiClient.AppImage`, and the release carries
`BragiClient-0.1.0-linux-full.nupkg` and `releases.linux.json` (the channel
manifest `UpdateService.cs` reads). So the build-and-publish path is
proven. An actual self-update from one installed build to the next still
needs a second release (`client-v0.1.1`) to exist before it can be
confirmed on a machine.

### Installing the tray app on a machine

The update check only runs for a Velopack-installed build. A
framework-dependent `dotnet build` output reports `IsInstalled == false`
and both the silent and manual checks no-op. So install the AppImage from
the release, not a local build:

```
gh release download client-v0.1.0 --repo NickMarcha/bragi \
  --pattern 'BragiClient.AppImage' --dir ~/.local/bin --clobber
chmod +x ~/.local/bin/BragiClient.AppImage
# point autostart at it:
#   Exec=/home/<user>/.local/bin/BragiClient.AppImage
```

Velopack self-updates by replacing that AppImage file in place, so it has
to live somewhere writable and stable. On the first run it logs to
`/tmp/velopack_BragiClient.log`; a line like `Initialised
LinuxVelopackLocator for BragiClient v0.1.0` followed by `Retrieving latest
release feed` confirms the update check is active.

## Dashboard presence heartbeat

`Tray/PeerPresenceClient.cs` holds a WebSocket open to sagepi's
`/ws/peer/{PEER_NAME}` (`app/main.py`/`app/peer_presence.py` server-side)
for as long as `LinkStatusService` reports the link Enabled - reconnects
with exponential backoff (2s up to 30s) if dropped, closes cleanly when
disabled. This is what drives the dashboard's real per-peer status dot
(green/red, not just "does a PipeWire node exist" - see the main
`README.md`'s "Per-card status dot" section for why that distinction
matters and how it was confirmed). Needs `PEER_NAME` and `BRAGI_WS_URL` in
`roc-link.env` (matching sagepi's `Peer.name` and its `tailscale serve`
URL) - both optional, missing either just disables the heartbeat without
affecting anything else. Verified end-to-end on sage-dev: disabling the
tray link flips the dashboard's sage-dev dot live, no page refresh.

## Status (2026-09-01)

- Systemd unit + hot-load mechanism: done, live in production on both
  sage-dev and sagedeck (verified: start/stop/crash-recovery, real audio
  confirmed working on sage-dev, nodes + default routing confirmed on
  sagedeck).
- Tray app on sage-dev: the `client-v0.1.0` AppImage is installed at
  `~/.local/bin/BragiClient.AppImage`, autostart points at it, and it runs.
  The Velopack log confirms the update check reaches the GitHub feed and
  correctly reports up to date. The earlier autostart entry pointed at a
  stale local `dotnet build` output, which is why the update check never
  did anything there.
- Tray app on sagedeck: still a local build. Builds fine, GUI not visually
  confirmed (couldn't launch over SSH, no display access). Install the
  AppImage the same way next time on the device.
- Velopack self-update: `client-v0.1.0` released 2026-09-01, workflow runs
  clean, the check works against the feed. The upgrade path itself is
  unconfirmed until a `client-v0.1.1` exists to update to.
