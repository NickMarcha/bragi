Native Kotlin Android client for Bragi over Tailscale.

- Select phone microphone or eligible device/app audio.
- Start the foreground audio service on the phone, then control streams from the Bragi web UI.
- Open the web UI from the app.
- Check GitHub for updates daily when opening the app, or check manually. Android confirms installation.

Install `BragiAndroid.apk`. Requires Android 10 or later and a connected Tailscale client.

This is an early release. Build, unit tests, and lint pass, but actual phone audio,
service lifecycle, and update installation have not yet been validated on a device.
The bundled Roc native libraries use 4 KiB alignment; 16 KiB-page phones are not
supported yet. Device-audio capture requires Android consent, and apps can block it.
Listening to the Pi microphone does not expose it as a microphone for other apps.
