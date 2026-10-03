Native Kotlin Android client for Bragi over Tailscale.

- Select phone microphone or eligible device/app audio.
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
