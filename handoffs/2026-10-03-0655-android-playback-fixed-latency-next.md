# Handoff: Android playback fixed, latency remains

Written 2026-10-03T06:55:57-05:00 from `/home/dev/src/bragi`.

## Current state

`main` is pushed through `1eaacda`. Android 0.1.7, versionCode 8, is released:
https://github.com/NickMarcha/bragi/releases/tag/android-v0.1.7

The user confirmed phone playback is now smooth, with noticeable delay. Their
latest logs confirm zero silent chunks after startup and zero Android playback
underruns for about a minute. Stop completed cleanly. No next implementation was
explicitly requested; the latest request was this handoff. Latency tuning is the
likely next topic, but preserve this working baseline.

The only untracked items before this handoff were `.github/workflows/client-tests.yml`
and `handoffs/`. Leave the workflow alone. This document is intentionally uncommitted.

## User decisions and completed scope

- Desktop tray has one top-level Volume action opening a window for all devices,
  plus Open web UI. It controls Bragi link volume. The user accepted the structure.
  See `864e104`, `5afb7c9`, and the desktop release artifacts.
- Android is native Kotlin. The user initially preferred C# sharing, then explicitly
  chose Kotlin after considering native audio access. Do not reopen that choice.
- Capture can select phone microphone or eligible device/app audio. Both were
  tested successfully on the FP5. Device audio capture worked with local media
  volume muted.
- Listening receives the same mixed audio as the Pi headset. Phone microphone can
  be selected instead of the headset mic for desktop microphone inputs.
- Exposing received audio as a microphone to arbitrary Android apps is deferred
  research, not a current requirement.
- GitHub APK releases and in-app update checks are established. Publishing through
  the release workflow is already authorized; do not ask again for routine releases.
- No ADB access. The user supplies Diagnostics logs through Pastebin.

The earlier handoff `2026-10-02-2218-managed-peers-host-units-then-android-app.md`
contains the host-unit cleanup history. That host work was completed, including
an approved PipeWire restart and restoration of VBAN. Do not repeat cleanup.

## Relevant artifacts

Read commits and existing documents instead of repeating their implementation:

- `038eee2`: Kotlin client and signed GitHub APK release workflow.
- `52ae3b4`: headset-monitor playback to Android and selectable desktop microphone
  routing. Server module `app/microphone.py` reconciles/persists the selected source.
- `6fa60e9`: Stop crash fix. Pooled TLS socket cleanup moved off the main thread;
  the regression test exercises a real connection/socket. Subsequent phone logs
  confirm clean Stop behavior.
- `aaa29e9`: diagnostics opens the structured Android GitHub issue form with
  prefilled version/device/logs and `bug`, `android` labels.
- `040b173`: persistent playback health measurements, Android 0.1.6.
- `1eaacda`: buffer-aware receiver latency and regression tests, Android 0.1.7.
- `android/RELEASE_NOTES.md`, `android/README.md`,
  `.github/workflows/android-release.yml`, `docs/android-stack-options.md`.

GitHub issue #1 is still open:
https://github.com/NickMarcha/bragi/issues/1
The user authorized reading/investigating it, but no GitHub comment or closure was
requested. Do not post messages without authorization. Some older README/backlog
text predates the completed Android work; do not mistake it for current state.

## Playback diagnosis and evidence

Two separate failures were established:

1. Bragi registered an obsolete phone Tailscale address. Sender audio worked, but
   receiver traffic went to an address absent from the tailnet. The user changed
   the phone address to its current Tailscale address and restarted. Playback began.
   Current phone address is `100.98.253.67`; discover it again rather than assume it
   never changes. The saved-IP field currently overrides fresh address detection,
   so stale saved addresses remain a possible UX improvement.
2. Playback was then chopped. 0.1.6 logs showed Android's 7108-frame buffer at
   44100 Hz, read gaps around 166 ms, about 69% fully silent chunks, and zero
   AudioTrack underruns. Roc used EXTERNAL clocking and only 40 ms target latency.
   Blocking AudioTrack writes paced Roc in large bursts.

A native Roc harness ran on the Pi against libroc 0.4.0. It sent nonzero L16 stereo
RTP over loopback every 221 frames and consumed 441-frame chunks. With 16 reads
per 160 ms burst, 40 ms latency produced 68.8% silent chunks. Even 10 ms read
pacing produced zero, as did the original burst schedule with the new buffer-aware
latency. This harness used the Pi's Roc library, not the Android bundled JNI;
phone confirmation supplies the actual Android evidence. Temporary harness files
were deleted after verification.

The fix is in `android/app/src/main/java/com/nickmarcha/bragi/PlaybackConfig.kt`.
Roc target latency is the AudioTrack buffer duration plus 40 ms network headroom,
rounded up in nanoseconds. The FP5 uses about 201 ms. Configuration regression
tests failed with the old 40 ms setting and passed with the fix. Sending is unchanged.

Latest phone evidence:
https://pastebin.com/raw/GYjgDFm6
Local copy: `/tmp/bragi-phone-playback-017.log`.
Earlier chopped-playback evidence:
https://pastebin.com/raw/RSgVt3vq
Local copy: `/tmp/bragi-phone-playback-016.log`.

Do not equate target latency plus Android buffer duration to measured end-to-end
latency. These logs do not measure that. If tuning next, investigate smaller
Android buffers or decoupled, evenly paced Roc reads, and build a real feedback
loop. Merely lowering the current target risks restoring the demonstrated chopping.

## Validation and release

0.1.7 passed 13 Android tests, build, and lint locally. GitHub release run
37120778476 passed release tests, lint, signing checks, and publication. Downloaded
APK checksum and stable signing certificate verified. The user installed/tested it.
No server changes were made during the playback fix, so server tests were not rerun;
the earlier baseline was 60 passing server tests.

Local Android checks:

```sh
JAVA_HOME=/home/dev/.cache/bragi-android/jdk-17.0.20.1+1 \
  android/gradlew -p android :app:assembleDebug :app:testDebugUnitTest :app:lintDebug --console=plain
```

SDK: `/home/dev/.cache/bragi-android/sdk`.
Server tests use `PYTHONPATH=/home/dev/.cache/bragi-pydeps/lib python3 -m pytest -q`.

Release by bumping both version fields and release notes, committing/pushing main,
and pushing the matching `android-vX.Y.Z` tag. Next version is 0.1.8/code 9.
The existing workflow signs and publishes `BragiAndroid.apk` and its checksum.
Signing secrets are configured. Private signing files are outside the repository
under `/home/dev/.local/share/bragi/android-signing/`; never print credentials.
`apksigner` needs the cached JDK bin directory on PATH.

Native dependency is `roc-android:0.2.1`; libraries have 4 KiB alignment. Rebuilding
for 16 KiB-page Android devices remains deferred. FP5 Android 15/API 35 works.

## Live-host tools and constraints

- Dashboard: `https://sagepi.tail08dfa.ts.net/`. Use T3 preview tools for browser work.
- SSH: `tailscale ssh sage@sagepi.tail08dfa.ts.net`.
- Set `XDG_RUNTIME_DIR=/run/user/$(id -u)` for remote PipeWire commands.
- Managed config/data is under `/home/sage/.local/share/bragi/data/`.
- FP5 outgoing sink `fp5-outgoing-sink`, uploaded mic `fp5-incoming-source`, playback
  monitor loopbacks `mic-to-fp5-capture` and `mic-to-fp5-playback`.
- Phone receive RTP/RTCP ports are 10041/10043; its sender targets Pi 10044/10046.
- `sudo -n` requires a password. Do not repeat attempts to capture with root.
- `tailscale debug capture --o -` works without sudo. Parse/filter in memory on the
  remote host and print only narrow metadata. Streaming full capture over the same
  SSH/Tailscale connection captures its own output and creates huge traffic.
- Pi outgoing RTP showed no missing sequence numbers and max send gaps about 10 ms.
- `rg` was unavailable; use it if installed, otherwise Python/grep/find.
- Main pushes auto-deploy the server via Komodo.

## Suggested skills

- `unslop` always applies, `.claude/skills/unslop/SKILL.md`.
- `diagnosing-bugs` for further latency/glitch work. Establish a measurable failure
  before speculative fixes; distinguish native harness evidence from phone evidence.
- `tdd` for meaningful regression tests when changing playback behavior.
- `research` if investigating Android audio APIs or rebuilding Roc.

Do not spawn subagents unless the user or applicable instructions explicitly asks
for delegation. Keep updates concise and do not request routine approvals already
given in this session. PipeWire service restarts require specific approval because
they interrupt audio; no restart is currently needed.
