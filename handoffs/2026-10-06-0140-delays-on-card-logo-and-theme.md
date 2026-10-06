# Handoff: delays on the phone card, logo and theme everywhere

Written 2026-10-06T01:40:40-05:00 from `/home/dev/src/bragi`.

Continues `handoffs/2026-10-06-0031-dashboard-redesign-vban-latency-done.md`. Its
user preferences and environment notes still apply and are not repeated here.

## Current state

`main` is pushed through `4bcbfcf` and deployed to sagepi. Server tests are green on
GitHub. Android 0.1.11 (versionCode 12) is released:
https://github.com/NickMarcha/bragi/releases/tag/android-v0.1.11

This session worked through items 1-5 of the previous handoff, then added the logo.
Read the commits for detail:

- `a0d4a23` Phones send `send_delay_ms` / `listen_delay_ms` in `peer_status`. The
  server accepts whole numbers from 0 to 5000 and drops anything else. The card
  shows "Sending 31 ms · Listening 102 ms". Desktop strips truncate the line, so
  the full text is in its tooltip (`docs/dashboard-ui.md`).
- `07c2c73` Android 0.1.10:
  - Sending-delay measurement from AudioRecord timestamps, with a `Capture health`
    Diagnostics line every 5 s. About 17 ms on the emulator.
  - Both delays are reported to the server when they move by 5 ms or more.
  - A detected Tailscale address replaces a stale saved one, on app open and on
    Start. This is unit-tested only, because debug builds always use 127.0.0.1.
  - Capture buffers were not changed (`android/README.md`, "Sending latency").
- `2925b30` The user deleted the old VBAN unit and binaries on sagepi. The backlog
  item is gone, and `docs/audio-bridge.md` notes that a rollback now needs the
  VBAN tools rebuilt from source.
- GitHub issue #1 closed with a note (user authorized).
- `6beb03e`, `4bcbfcf` The user's logo is in `docs/brand/bragi-logo.png`.
  `dev/brand.py` (run in `python:3.13-slim` with pillow, command in its docstring)
  cuts out the background and generates every icon:
  - web favicon, touch icon and header mark
  - Android adaptive launcher icon (with a monochrome layer), notification icon
    `ic_stat_bragi` and header mark
  - desktop tray icons: colour, grey, and colour with a red error badge

  The dashboard palette and the Android theme now share the logo's colours:
  `#111317` background and `#00c0b0` / `#1ff1eb` accents (`app/static/style.css`
  `:root`, `android/app/src/main/res/values/colors.xml`). The user approved the
  screenshots before the push.

Untracked and not ours: `.github/workflows/client-tests.yml`. Handoff files stay
uncommitted.

## Waiting on the user

- **FP5 numbers from 0.1.10 or later.** Diagnostics after a minute of sending
  (`Capture health ... delayMs=`), plus the card's figures. These decide what to
  shrink: phone capture buffers, or more likely the Pi's Roc receiver
  `sess.latency.msec = 40` and the network. Measure before changing anything, as in
  0.1.9.
- **Delays on the desktop card.** They only show on hover at desktop widths. The
  user hasn't said whether they want them visible there, which needs a new spot on
  the card.
- **Parked peers.** The user hasn't been asked yet whether they want a way to
  reach a parked peer's controls.
- **Touchscreen check.** The drag grip and horizontal faders still need a real
  phone.
- **Tray icons on a desktop.** The new icons are unseen until the Avalonia client
  is rebuilt and reinstalled on sagedeck or sage-dev.

## Optional follow-ups I offered, not taken up

- A deeper teal for the fader fills, keeping the bright teal for dots and buttons.
- A teal Start audio service button in the app.
- The small arrow at the bottom of the beard in the logo may be an artifact of how
  the image was made. Fixing the source and rerunning `dev/brand.py` updates every
  icon.

## Longer term

`docs/backlog.md` still lists:
- Roc jitter headroom
- Pi-side delay measurement
- the listen-only status wording
- 16 KiB-page Roc libraries
- F-Droid
- the Windows tray client
- dual-headset playback

## Suggested skills

- `unslop` always applies.
- `tdd` for behaviour changes. Server tests run with
  `PYTHONPATH=/tmp/bragideps python3 -m pytest`. Android unit tests cover the
  delay, address and playback helpers.
- `diagnosing-bugs` for the latency work once FP5 numbers arrive.
- `codebase-design` if `BragiService`'s status and delay reporting grows further.

Do not spawn subagents unless asked.

## Added 2026-10-06 ~07:10: issue #2, no audio when listening on the FP5

**Resolved and closed, see the 10:12 section at the end.** Kept for the record.

Resume this from a machine that can reach sagepi. The sandbox can't.

https://github.com/NickMarcha/bragi/issues/2 is open. It was filed from 0.1.11 on the FP5 with
Android 15: "Receiving Bragi audio", no sound with Bluetooth media volume at full, and still
none after several restarts.

What the phone log shows:
- Playback runs normally: 240000 frames every 5 s, no underruns, delay about 104 ms
  (roc=60 output≈44).
- Every chunk is zero: `silentChunks=500/500`, `peak=0.0`. Roc puts out zeros both when no
  packets arrive and when the packets carry silence, so this log can't tell the two apart.
- `route=2:FP5` means the built-in speaker. A2DP would be 8, so even real audio may not have
  gone to Bluetooth. Look at this once audio is confirmed reaching the phone.
- The issue's excerpt held only health lines, so `Receiver bound to …` (the bound address)
  was cut off.

The user ran `pw-top` on sagepi while the phone was listening. `fp5-outgoing-sink` was running,
fed by `mic-to-fp5-capture` → `mic-to-fp5-playback` (the headset playback monitor, see
`docs/audio-bridge.md` "Android headset routing"). It had 25 errors at 44.1 kHz, and the
HyperX output was driving the graph at 48 kHz. Not related to this issue: `sagedeck-audio`
and `sagedev-audio` show ERR around 110826.

Still to check, in this order:
1. Was anything actually playing into the HyperX? The phone only mirrors the headset. Play
   something from a desktop that's audible in the headset and see whether the phone plays it.
2. Is it a stale IP again, as in #1? Check the `remote.ip` in the fp5 block of
   `data/peers.conf` on sagepi against `tailscale status` and the phone's `Receiver bound to`
   line.
3. If both are fine, get the full phone log (Diagnostics → Copy logs).

Close #2 only with the user's OK, as was done for #1.

### Uncommitted: shorter issue excerpts

**Superseded.** It was rewritten on the Windows machine and released in 0.1.13.
Discard the uncommitted copy on the machine that wrote this; pushing it would conflict.

`android/app/src/main/java/com/nickmarcha/bragi/DiagnosticsReport.kt` now has
`collapseHealthLines`. Each run of `Playback health` / `Capture health` lines keeps the first
and last line of each kind, with `… N health lines omitted …` between them, so startup events
fit in the 2500-character excerpt. The new test is in `DiagnosticsReportTest`. All unit tests
and lint pass (`./gradlew --offline :app:testDebugUnitTest :app:lintDebug`). It's not
committed or released yet. The user hasn't decided between shipping it as 0.1.12 now or
together with the #2 fix. Remember to bump `versionName` / `versionCode` in
`app/build.gradle.kts` when it goes out.

A separate UX note: "Report on GitHub" opens GitHub's form in the browser, and the user has
to be signed in there before they can submit. The user hit this the first time.

## Added 2026-10-06T10:12+02:00: #2 fixed, 0.1.12 and 0.1.13 out

Written from `C:\Users\Nicol\Desktop\bragi` (Windows, "sage"). This machine reaches
sagepi over Tailscale and has the FP5 on wireless adb (`adb connect 192.168.1.8:<port>`;
the port changes whenever wireless debugging is toggled, `adb mdns services` finds it).
Tailscale SSH to sagepi is refused for user `Nicol`, and the user doesn't want that
changed. T3's device panel is off on purpose; use plain `adb`.

### What happened

Cause of #2: since 0.1.10 the app took the first 100.64.0.0/10 address on any interface.
On the FP5 the carrier's `rmnet_data1` (100.83.223.246) is listed before Tailscale's `tun0`
(100.98.253.67), so the app saved and registered the carrier address. Confirmed with
`adb shell ip -4 -o addr`. The issue's closing comment has the details.

Commits, all pushed except the last:
- `def1b5a` Address detection reads `tun*` interfaces only. Released as 0.1.12
  (`78baf8f`). The user confirmed listening works on the FP5 and the other Android peer.
- `faa4039` Server: the phone card warns "Phone is at X, Bragi sends to Y" when the
  Android control socket's `X-Forwarded-For` (set by `tailscale serve`) differs from the
  registered address. Deployed to sagepi.
- `7202fa9` Android: `receivedKiB` in Playback health lines, `collapseHealthLines` for
  issue excerpts, JDK note in `android/README.md`. Released as 0.1.13 (`74ce664`). Both
  phones run 0.1.13; the FP5 log showed `receivedKiB≈900` per 5 s with real peaks.
- `f8bd8ee` (**not pushed, not released**) Stop no longer logs "Receiver worker failed"
  with a stack trace: pausing the track mid-write returned 0 and was treated as a failure.

### Open

- **The address warning is untested on sagepi.** Whether `tailscale serve` really sends
  `X-Forwarded-For` is assumed, not seen; without it the warning never shows. It also
  keeps one seen address per peer name and the latest connection wins, so a stray
  socket under a phone's name leaves a false warning until the phone reconnects. Fix
  that before testing with a fake `?client=android` socket from another tailnet machine.
- `f8bd8ee` needs a push and goes out with the next Android release.
- `route=2:FP5` (built-in speaker) in the log was expected: the user was on speaker.
- The "Waiting on the user" list above still stands, minus the FP5 latency numbers if the
  log in this session is enough: delay 104 ms (roc=60 output=44), no underruns.

### Environment notes for Windows

- Gradle needs Android Studio's JDK: `JAVA_HOME="/c/Program Files/Android/Android Studio/jbr"`,
  then `./gradlew.bat :app:testDebugUnitTest :app:lintDebug` in `android/`.
- Server tests: `python -m pytest -p no:cacheprovider --basetemp=<fresh dir>`.
- Python's `Path.write_text` uses cp1252 here and mangled a `…`; write UTF-8 bytes
  explicitly or use the Edit tool.
