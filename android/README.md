# Bragi Android

Native Kotlin client for Android 10 or later. Connect Tailscale, enter the HTTPS
Bragi URL and the phone's Tailscale IPv4 address, choose a source, and start the
audio service. Bragi registers the peer and supplies its Roc ports and desired
stream state. The web UI can pause sending and enable headset-microphone playback
while the service is running. Receiving starts disabled for a newly registered peer.

Microphone capture uses AudioRecord. Device audio uses AudioPlaybackCapture and
requires Android's MediaProjection consent each time the service starts. Apps can
block capture, and calls are not guaranteed to be capturable. Change sources by
stopping and restarting. The persistent notification has a Stop action. After
process death, open the app and start again. It does not start capture on boot.
Listening to the Pi microphone plays it through AudioTrack; it does not make it
available as another app's microphone.

## Build and checks

Use JDK 17 and Android SDK 35 with build-tools 35.0.0. Set `ANDROID_HOME` or put
`sdk.dir=/path/to/sdk` in ignored `local.properties`. From this directory:

```sh
./gradlew :app:assembleDebug :app:testDebugUnitTest :app:lintDebug
```

The debug APK is `app/build/outputs/apk/debug/app-debug.apk`. It has a debug signing
identity and cannot update to an APK signed with the production key.

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

## Native dependency limits

The app uses the MIT-licensed Maven Central `roc-android:0.2.1` AAR with its bundled
libroc, JNI bridge, and C++ runtime. It sends 44.1 kHz stereo plain RTP and RTCP,
without FEC. Mono microphone samples are duplicated into stereo. The receiver binds
to the phone's Tailscale IP. Protocol compatibility with the deployed Pi still needs
an actual device test.

Inspection found 4 KiB ELF segment alignment in the bundled arm64 and x86_64 native
libraries. Treat 16 KiB-page devices as unsupported until the native libraries are
rebuilt and tested. A source-built Roc AAR and reproducible APK verification are
also needed before pursuing F-Droid distribution. Current dependencies do not use
Google Play Services.

## Device validation still needed

Build, JVM unit tests, and Android lint pass. No usable Android device was connected
for the initial implementation. Before publishing:

- Test microphone and eligible app audio through the Pi headset over Tailscale.
- Test receiver playback, pause/resume from the web UI, and source changes.
- Test screen-off operation, notification Stop, network loss/reconnect, and rapid stop/start.
- Revoke capture consent, kill the process, and remove the peer from Bragi. Verify
  capture stops and errors explain how to recover.
- Install a signed test APK, publish a newer test release, and verify discovery,
  download, install consent, and preservation of saved settings.
- Confirm native-library loading on the target phone's page size.
