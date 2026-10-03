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
