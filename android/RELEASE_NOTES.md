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

## 0.1.9 lower listening delay

The app now shows the listening delay measured on the phone: Roc's buffer plus
Android's output delay. Network and Pi delay come on top and are not measured.

Playback now asks Android for a low-latency track at the phone's native sample
rate instead of the default media path, which drained the FP5's 161 ms buffer in
166 ms bursts. Only 20 ms of the track buffer is used at first. If Android
reports underruns, the buffer grows by half and Roc's buffer is rebuilt to match,
so the first second or two of listening may glitch while it settles. On the
emulator the phone-side delay fell from 252 ms to 169 ms with no underruns after
settling. FP5 results still need confirming; the Diagnostics log shows each
step and a delayMs value every 5 seconds.

## 0.1.10 sending delay and fresh Tailscale address

While sending, the app shows the sending delay measured on the phone: how long
captured audio waits before it reaches Roc. Diagnostics records a Capture health
line every 5 seconds with `delayMs`. On the emulator it reads about 17 ms. Network
and Pi delay come on top and are not measured. Nothing about capture itself
changed in this release; the number is there to show where the sending delay goes.

The phone now reports both delays to Bragi, and the web UI shows them on the
phone's card, for example "Sending 31 ms · Listening 102 ms". This needs the
matching Bragi server update; older servers ignore the numbers.

If Tailscale gives the phone a new address, the app now uses it instead of the
saved one, both when the app opens and when the service starts, and says so in
a short message. A stale saved address is what stopped listening in issue #1.
The address field is still used when no Tailscale address can be detected.


## 0.1.11 new logo

The app has a new icon and a dark look in the logo's colors, to match the
Bragi web UI. The notification shows the logo's outline. Nothing about audio
changed since 0.1.10.

## 0.1.12 listening on mobile data

The app now takes its Tailscale address only from Tailscale's own connection.
Mobile carriers can hand out addresses in the same 100.x range, and 0.1.10 and
0.1.11 could pick that one instead, save it, and send it to Bragi. Listening
then stayed silent (issue #2). Open the app once after updating so it saves the
right address.
