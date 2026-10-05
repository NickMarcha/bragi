Native Kotlin Android client for Bragi over Tailscale.

- Select phone microphone, eligible device/app audio, or none to only listen.
- Start the foreground audio service on the phone, then control streams from the Bragi web UI.
- Open the web UI from the app.
- Check GitHub for updates daily when opening the app, or check manually. Android confirms installation.

Install `BragiAndroid.apk`. Requires Android 10 or later and a connected Tailscale client.

This is an early release. Build, unit tests, and lint pass, but service lifecycle and update installation still need device validation.
The bundled Roc native libraries use 4 KiB alignment; 16 KiB-page phones are not
supported yet. Device-audio capture requires Android consent, and apps can block it.
Listening to the Pi microphone does not expose it as a microphone for other apps.

## 0.1.1 diagnostics

Open Diagnostics and use Copy logs to report problems without ADB. Recent service,
connection, and audio-worker events are stored privately on the phone. Java crash
stacks are recorded, and Android process-exit details and available native crash
traces are collected on the next launch on Android 11 or later.

Microphone and device-audio sending were confirmed working by the user on 0.1.0.
The reported Stop crash is still under investigation. Routing phone microphone
input to desktops and headset playback audio to the phone is being implemented.

## 0.1.2 headset routing

The Listen to Bragi audio button receives the same audio mix as the Pi headset.
In the web UI, Microphone for desktops selects the Pi headset microphone or a
phone running in microphone mode. Phone microphone capture feeds microphone
input rather than headset playback; device-audio capture still feeds playback.
The Stop issue remains under investigation. Diagnostics and Copy logs remain
available.

## 0.1.3 Stop crash fix

Stop no longer closes network sockets on Android's main thread. The reported
NetworkOnMainThreadException came from closing a pooled TLS connection in service
teardown. Connection cleanup now runs on the IO dispatcher, alongside asynchronous
audio shutdown. A regression test opens a real pooled connection and checks that
its socket is closed off the lifecycle thread. Diagnostics remain available.

## 0.1.4 diagnostic reporting

Diagnostics now has Clear logs and Report on GitHub. Reporting opens a prefilled
issue with app/device details and a recent log excerpt, and copies the complete
log for optional pasting. Review and submit the issue in your browser. The Stop
crash fix from 0.1.3 is included.

## 0.1.5 structured issue reports

Report on GitHub opens an Android bug form with a problem-area dropdown and
required description, reproduction steps, and expected behavior. App version,
device details, and recent diagnostics are filled in. Submitted reports receive
bug and android labels automatically.

## 0.1.6 playback diagnostics

Diagnostics records the receiver address and playback buffer size, then reports
decoded audio levels, silent chunks, read timing, Android playback underruns,
played frames, and output route every five seconds while listening. These
measurements help investigate choppy playback reported in issue #1. No audio
recordings are stored or uploaded. Playback timing is unchanged in this build.

If sending works but listening is silent, check that the phone address in Bragi
matches the current IPv4 address shown by Tailscale.

## 0.1.7 playback buffering fix

Listening now gives Roc enough buffered audio to cover Android's playback buffer,
plus 40 ms of network headroom. On the FP5, Android's blocking writes returned in
bursts roughly 166 ms apart, exceeding the previous fixed 40 ms receiver latency.
Phone diagnostics showed about 69% silent chunks despite zero AudioTrack
underruns. A native Roc reproduction produced 68.8% silent chunks at 40 ms and
none with the buffer-aware latency.

The FP5 now uses about 201 ms of Roc buffering. This increases listening delay;
devices with smaller playback buffers use less. Sending is unchanged. Playback
diagnostics remain available. Phone confirmation of the fix is still needed.

## 0.1.8 listen-only mode

Choose None (listen only) as the audio source to hear Bragi audio without sending
anything. The app skips the microphone permission and the capture approval, and
Bragi turns listening on for the phone and hides its send control in the web UI.
This needs the matching Bragi server update; older servers reject the mode.
