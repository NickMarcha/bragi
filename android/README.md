# Bragi Android

Native Kotlin client for Android 10 or later. Connect Tailscale, enter the HTTPS
Bragi URL and the phone's Tailscale IPv4 address, choose a source (phone
microphone, device audio, or none to only listen), and start the audio service. Bragi registers the peer and supplies its Roc ports and desired
stream state. The web UI can pause sending and enable headset audio playback
while the service is running. Receiving starts disabled for a newly registered peer,
except in listen-only mode, where Bragi turns it on and keeps sending off.

Listen-only mode needs no microphone permission or capture consent.
Microphone capture uses AudioRecord. Device audio uses AudioPlaybackCapture and
requires Android's MediaProjection consent each time the service starts. Apps can
block capture, and calls are not guaranteed to be capturable. Change sources by
stopping and restarting. The persistent notification has a Stop action. After
process death, open the app and start again. It does not start capture on boot.
The Listen to Bragi audio button plays the same mix as the Pi headset, through
AudioTrack. In the web UI, Microphone for desktops selects the Pi headset mic or a
phone running in microphone mode. Phone microphone input is not played through
the headset; device-audio input still is. A selected unavailable microphone stays
silent. Android itself does not expose received audio as another app's microphone.

## Playback latency

While listening, the status line shows the delay measured on the phone: Roc's
target latency plus Android's output delay, read from AudioTrack timestamps. Time
on the network and on the Pi comes on top and is not measured. Diagnostics records
`delayMs=... (roc=... output=...)` in every Playback health line.

Playback uses a low-latency AudioTrack at the output's native sample rate; Roc
resamples from Bragi's 44.1 kHz. Until 0.1.8 it used the default media path, which
on the FP5 drained a 161 ms buffer in 166 ms bursts and needed about 200 ms of Roc
buffering to avoid chopping. The app now uses 20 ms of the track buffer and grows
it by half whenever Android reports underruns. Each growth rebuilds the Roc
receiver so its target stays at the buffer plus 40 ms of network headroom. Never
lower the Roc target below the playback buffer: that is what chopped in 0.1.6.

Measured phone-side delay:

| Build | Device | Delay |
| --- | --- | --- |
| 0.1.7 | FP5 | ~400 ms (Roc 201 ms, 161 ms buffer, plus output) |
| 0.1.9 | FP5 | ~102 ms (Roc 60 ms, output 42 ms), buffer never grew |
| 0.1.9 | Emulator, low latency not granted | 169 ms, after one growth to 30 ms |

## Build and checks

Use JDK 17 and Android SDK 35 with build-tools 35.0.0. Set `ANDROID_HOME` or put
`sdk.dir=/path/to/sdk` in ignored `local.properties`. From this directory:

```sh
./gradlew :app:assembleDebug :app:testDebugUnitTest :app:lintDebug
```

The debug APK is `app/build/outputs/apk/debug/app-debug.apk`. It has a debug signing
identity and cannot update to an APK signed with the production key.

Debug builds talk only to a local dev server (`10.0.2.2`, `127.0.0.1`, or
`localhost`, over cleartext allowed by `src/debug/`), register as `127.0.0.1`, and
refuse the production URL. They skip the daily update check. See
[`../dev/README.md`](../dev/README.md) for the emulator loop. Release builds accept
only HTTPS and Tailscale addresses.

## GitHub updates and releases

The app checks public GitHub releases once a day when opened. The Check for updates
button also checks manually. There is no background update service. Only stable
`android-vX.Y.Z` releases with a `BragiAndroid.apk` asset and GitHub's SHA-256 asset
digest qualify. Desktop tags, drafts, and prereleases are skipped.

After download, the app checks the digest, package ID, increasing versionCode, and
exact installed signing identity. Android may ask the user to allow installs from
Bragi and always handles final installation confirmation. Silent installation is
not supported. Keep the same signing key for every GitHub release; key rotation is
not implemented. Increase both versionName and versionCode in `app/build.gradle.kts`.

Keep the keystore outside the repository and back it up securely. Set these private
environment variables before building:

- `BRAGI_ANDROID_KEYSTORE`: absolute keystore path.
- `BRAGI_ANDROID_STORE_PASSWORD`: keystore password.
- `BRAGI_ANDROID_KEY_ALIAS`: signing alias.
- `BRAGI_ANDROID_KEY_PASSWORD`: key password.

Run `./release.sh` to build and verify a signed APK. The script creates
`app/build/outputs/release/BragiAndroid.apk` and its checksum. It does not publish.
Without signing variables, Gradle's ordinary release build produces an unsigned APK.

The GitHub workflow `.github/workflows/android-release.yml` builds, runs release
unit tests and lint, verifies the signature, and publishes the APK and checksum
when an `android-vX.Y.Z` tag is pushed. It rejects tags that disagree with versionName.
Android releases do not replace the desktop release as GitHub's latest release.
Review `RELEASE_NOTES.md` before each release, and increase versionCode as well as
versionName. To release a committed version:

```sh
git push origin main
git tag android-v0.1.0
git push origin android-v0.1.0
```

Repository Actions secrets are `BRAGI_ANDROID_KEYSTORE_BASE64`, containing the
base64-encoded keystore, plus the store password, alias, and key password variables
listed above. The workflow restores the key in the runner's temporary directory
and removes it after the build. Keep a secure backup outside GitHub too.
The initial signing key is stored privately outside this checkout.

## Diagnostics

Open Diagnostics and tap Copy logs. Logs are stored privately and capped at about 256 KiB. They
include app/device versions, service events, Java exception stacks, and Android
process-exit details on Android 11 or later. Native crash/ANR traces are included
when Android makes them available. Clear logs erases the stored log. Report on
GitHub opens the Android bug form with app version, device details, and a bounded
recent log excerpt, and copies the full log to the clipboard. The form requires a
problem area, description, reproduction steps, and expected behavior. GitHub adds
`bug` and `android` labels automatically. Review and submit the issue in
the browser, pasting the full log if needed. There is no automatic issue submission
or background log upload.

## Native dependency limits

The app uses the MIT-licensed Maven Central `roc-android:0.2.1` AAR with its bundled
libroc, JNI bridge, and C++ runtime. It sends 44.1 kHz stereo plain RTP and RTCP,
without FEC. Mono microphone samples are duplicated into stereo. The receiver binds
to the phone's Tailscale IP. It works with the Pi's PipeWire Roc modules (Roc 0.4.0).

Inspection found 4 KiB ELF segment alignment in the bundled arm64 and x86_64 native
libraries. Treat 16 KiB-page devices as unsupported until the native libraries are
rebuilt and tested. A source-built Roc AAR and reproducible APK verification are
also needed before pursuing F-Droid distribution. Current dependencies do not use
Google Play Services.

## Device status

Confirmed on a Fairphone FP5 (Android 15):

- Microphone and eligible device-audio sending through the Pi headset (0.1.0).
  Device-audio capture continued while local playback was muted.
- Clean Stop, after 0.1.3 moved TLS socket cleanup off the main thread.
- Listening to the headset mix without chopping (0.1.7), and at about 100 ms of
  phone-side delay (0.1.9).
- Installing GitHub releases over earlier ones.

Not yet checked on a phone:

- Listen-only mode against the deployed Pi (0.1.8 and later).
- Screen-off operation, network loss and reconnect, rapid stop and start.
- Revoking capture consent, killing the process, and removing the peer from Bragi.
  Capture should stop and errors should explain how to recover.
- Native-library loading on a 16 KiB-page phone.
