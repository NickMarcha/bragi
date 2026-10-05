# Local Android debugging

Drive the Android app from an emulator against a Bragi server on this
machine, with no Tailscale and no Pi. The server is the real app on the fake
PipeWire from the tests, so registration, the dashboard, and the peer
control socket are real; the audio graph is not. Audio is checked with the
Roc 0.4 CLI tools (`roc-toolkit-tools` from Debian trixie, as on sagepi)
standing in for sagepi's Roc modules.

```
dev/android.sh up        # dev server on 127.0.0.1:20080, UDP redirects, debug APK installed
dev/android.sh tone 10   # stream a 440 Hz tone to the phone
dev/android.sh record 10 # record what the phone sends
dev/android.sh logs      # the app's diagnostics log ("Playback health" lines show peak and silence)
dev/android.sh down
```

The dashboard is at http://127.0.0.1:20080/. It starts empty on every
`up`; the peer registry lives in a tmpfs.

## How the pieces connect

| Path | Route |
| --- | --- |
| App to server | `http://10.0.2.2:20080` (the emulator's alias for host loopback) to the `server` container |
| Phone sending | `10.0.2.2:20144`/`20146` to the `recv` container, published on loopback |
| Phone listening | `send` container to `10.0.2.2:20141`/`20143` (host loopback from a container), then `adb emu redir` into the emulator |

The phone registers as `127.0.0.1`, which the server accepts only with
`BRAGI_LOCAL_DEV=1`. For a loopback peer the app binds its receiver to all
interfaces, because the redirected packets arrive on the emulator's own
address. `BRAGI_ROC_PORT_BASE=20141` puts the first Android peer on
20141-20146; a second peer would get 20151-20156 and need its own redirects.

The `send` container reaches host loopback only when rootless Docker allows
it: `DOCKERD_ROOTLESS_ROOTLESSKIT_DISABLE_HOST_LOOPBACK=false` in a drop-in
under `~/.config/systemd/user/docker.service.d/`.

## Debug build limits

Debug builds talk only to `10.0.2.2`, `127.0.0.1`, or `localhost`, over
cleartext allowed by `android/app/src/debug/res/xml/network_security_config.xml`,
and register only as `127.0.0.1`. They refuse the production server URL, so a
debug build cannot touch sagepi by accident. Release builds are unchanged:
HTTPS and Tailscale addresses only. Debug builds skip the daily update check,
since a release APK cannot replace a debug install.

The emulator's microphone is silent unless the emulator is given host audio
input, so `record` with the microphone source proves the packets arrive (a
Roc session appears) but records silence.
