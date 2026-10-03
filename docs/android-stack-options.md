# Android stack options

Research checked 2026-10-03. A Kotlin app is implemented under `android/`.
Build, unit tests, and lint pass; real-device audio remains unverified.

## Recommendation

### User decisions

The user chose native Kotlin. The app uses Android AudioRecord, AudioTrack,
MediaProjection, a foreground service, and roc-java directly. The C# comparison
below records the earlier research.

The first version should send either eligible device playback or the phone
microphone, selectable by the user. Device playback remains subject to Android's
capture policy and MediaProjection consent.

Using the Pi headset microphone as another Android app's microphone is deferred
to backlog research. It is not a requirement for the first version.

Early releases will be APKs published on GitHub. F-Droid remains a future goal.
Keep a stable package ID and signing key so later releases can update an
installed app.

The app checks GitHub daily on opening and offers a manual check. It selects
stable `android-vX.Y.Z` releases, verifies the APK checksum and installed signing
identity, then opens Android's installer. See [`../android/README.md`](../android/README.md).

### Initial engineering recommendation

Use native Kotlin with roc-java for the first Android prototype. The hardest parts
are Android audio capture, foreground-service lifetime, and Roc interoperability.
Kotlin can call the existing Java bindings directly. Avalonia can share the desktop
volume UI and protocol models, but still needs Android-specific audio and service
code plus a Roc binding. This is an engineering recommendation, not a claim that
Avalonia cannot work or cannot qualify for F-Droid.

Decide what audio the app must carry before choosing the final stack. A normal
Android app cannot promise the same virtual microphone and unrestricted system
playback routing as the Linux PipeWire client.

## Verified facts

### Roc bindings

roc-java supports Kotlin and Android. Its Android AAR includes libroc; upstream also
documents building the AAR and native library from source. Its documented minimum
runtime is Android 10, API 29. [1]

The binding exposes configurable endpoint URIs, `RocSender.connect`,
`RocReceiver.bind`, and separate `AUDIO_SOURCE`, `AUDIO_REPAIR`, and
`AUDIO_CONTROL` interfaces. The README demonstrates an RTCP control endpoint.
`FecEncoding.DISABLE` and plain `rtp://` are supported. The old Roc Droid app's
hardcoded ports and RS8M choice are not limitations of the current binding. [1][2]
For Bragi's current disabled-FEC configuration, use plain RTP and omit repair
connections; allocated repair ports can remain reserved. Actual interoperability
with the Pi's installed Roc/PipeWire version still needs a device test.

### Android audio and service lifetime

A microphone foreground service needs `RECORD_AUDIO`, the microphone service type,
and its foreground-service permission. Android's while-in-use rules prevent ordinary
background creation of that service. Starting it from a visible activity allows it
to continue microphone capture in the background. Remote commands cannot be relied
on to recreate it after process death. Media playback has its own service type.
Apps targeting Android 15 or later cannot start media-playback or media-projection
foreground services from `BOOT_COMPLETED`. [3][4]

Sending the phone's microphone is different from sending other apps' audio.
`AudioPlaybackCapture` requires `RECORD_AUDIO`, a user-approved MediaProjection,
and the same user profile as the source app. Only media/game/unknown usages with
an allowing capture policy qualify. Apps can forbid capture. This does not promise
capture of calls or all phone audio. MediaProjection needs its own foreground-service
type and consent flow. [3][5]

Receiving PCM and playing it through `AudioTrack` makes it audible on the phone.
It does not install a virtual microphone for another app. AOSP's audio-policy
injection path uses system audio policy; `MODIFY_AUDIO_ROUTING` is protected and
explicitly marked unavailable to third-party applications. The MediaProjection
exception permits playback capture, not general microphone injection. A normal
F-Droid-installable app should therefore not promise to route the Pi headset mic
into arbitrary Android calling apps. Root, privileged system integration, hardware
routing, or cooperation from the consuming app would be separate designs. [6]
Android also arbitrates concurrent microphone capture; two ordinary apps cannot
both capture microphone input at the same time under the documented policy. [7]

### What Avalonia would share

Avalonia officially supports Android through a .NET Android workload and Android
SDK. C# can call the Android `AudioRecord` binding, including its recording APIs.
Kotlin does not gain extra audio permissions or bypass platform restrictions. [12]
.NET Android can wrap Java AAR libraries with managed JNI wrappers. These
establish a feasible route to roc-java, but do not verify a working Roc binding. [8][9]

The desktop project currently targets `net10.0` and references `Avalonia.Desktop`
and Velopack. Its audio controller shells out to `systemctl`; it contains no Roc
PCM processing to reuse. `VolumeState`, server-address handling, and parts of the
WebSocket clients are candidates for a shared C# library. The volume window can be
adapted for Android; the tray, systemd controller, autostart, PipeWire checks, and
updater need platform replacements. This assessment comes from
`client/src/Bragi.Client/Bragi.Client.csproj`, `Volume/`, `Config/`, and `Tray/`.

### F-Droid and updates

F-Droid requires FLOSS app code, dependencies, assets, and build tools, with no
proprietary Google Play Services dependencies. Its current policy lists trusted
Maven repositories and some SDKs as permitted sources for prebuilt FLOSS binaries.
An artifact's presence on Maven Central alone does not prove that it qualifies. [10]

Reproducibility is a build result to prove. F-Droid can reproduce an upstream APK,
copy its signature, and distribute it when the rebuilt content matches. This offers
a path to using the same signing identity for direct releases and F-Droid updates;
plan the signing key and package ID before publishing. Neither language guarantees
reproducibility. [11]

## Implementation limits

The app pins the published `roc-android:0.2.1` AAR. Its API differs from the current
upstream README. Inspection of the APK's arm64 and x86_64 native libraries found
4 KiB ELF segment alignment. Treat 16 KiB-page devices as unsupported until the
native libraries are rebuilt and validated. F-Droid source-build provenance and
reproducibility are still pending.

## Unknowns and decision gates

- Validate selectable eligible phone playback and phone microphone capture.
  Arbitrary-app virtual microphone support is deferred to research.
- Pin roc-java/libroc revisions and test plain RTP, RTCP, ports, formats, routing,
  latency, and stop/start on the actual Fairphone over Tailscale. Source review does
  not prove this combination works.
- Test remote toggling inside the existing service, screen-off operation, process
  death recovery, and capture-consent loss on the actual Android version.
- Check F-Droid acceptance and a reproducible release build early. Native Kotlin
  still needs a dependency/license audit and Roc native-build recipe. The cited
  policy does not list NuGet among prebuilt-binary exceptions; acceptance of the
  .NET workload, runtime packs, and Avalonia dependency chain is unverified. This
  research did not establish a blanket prohibition on .NET/Avalonia apps.
- Establish signing and versioning for the chosen GitHub APK release route.
  The Linux Velopack feed is not the Android update plan.

## Sources

[1]: https://github.com/roc-streaming/roc-java/blob/main/README.md
[2]: https://github.com/roc-streaming/roc-java/tree/main/src/main/java/org/rocstreaming/roctoolkit
[3]: https://developer.android.com/develop/background-work/services/fgs/service-types
[4]: https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start
[5]: https://developer.android.com/media/platform/av-capture
[6]: https://github.com/aosp-mirror/platform_frameworks_base/blob/master/media/java/android/media/audiopolicy/AudioPolicy.java
[7]: https://developer.android.com/media/platform/sharing-audio-input
[8]: https://github.com/AvaloniaUI/avalonia-docs/blob/main/docs/platform-specific-guides/android/android.md
[9]: https://learn.microsoft.com/en-us/dotnet/android/binding-libs/binding-java-libs/
[10]: https://f-droid.org/docs/Inclusion_Policy/
[11]: https://f-droid.org/docs/Reproducible_Builds/
[12]: https://learn.microsoft.com/en-us/dotnet/api/android.media.audiorecord?view=net-android-35.0

The protected permission declaration cited for microphone injection is in
[AOSP AndroidManifest.xml](https://github.com/aosp-mirror/platform_frameworks_base/blob/master/core/res/AndroidManifest.xml).
