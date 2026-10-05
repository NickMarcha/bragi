#!/usr/bin/env bash
# Local debug loop for the Android app: see dev/README.md.
#   dev/android.sh up              start the dev server, add the emulator's UDP redirects, install the debug APK
#   dev/android.sh tone [seconds]  stream a 440 Hz tone to the phone (test Listen)
#   dev/android.sh record [secs]   record what the phone sends, then summarise it (test sending)
#   dev/android.sh logs            the phone's diagnostics log
#   dev/android.sh down            stop the containers and remove the redirects
set -euo pipefail
cd "$(dirname "$0")/.."
SERIAL=${ANDROID_SERIAL:-emulator-5554}
compose() { docker compose -f dev/compose.yml --profile audio "$@"; }
# The first Android peer registered with the dev server gets these (BRAGI_ROC_PORT_BASE=20141).
PHONE_PORTS=(20141 20143)

case "${1:-}" in
  up)
    compose build -q
    compose up -d server
    for port in "${PHONE_PORTS[@]}"; do
      adb -s "$SERIAL" emu redir del "udp:$port" >/dev/null 2>&1 || true
      adb -s "$SERIAL" emu redir add "udp:$port:$port" >/dev/null
    done
    (cd android && ./gradlew -q assembleDebug)
    adb -s "$SERIAL" install -r android/app/build/outputs/apk/debug/app-debug.apk >/dev/null
    echo "Dev server: http://127.0.0.1:20080/ (app: http://10.0.2.2:20080/, peer IP 127.0.0.1)"
    ;;
  tone)
    SECONDS_TO_SEND=${2:-10} compose run --rm --no-deps send
    ;;
  record)
    mkdir -p dev/out
    rm -f dev/out/from-phone.wav
    compose up -d --force-recreate recv
    sleep "${2:-10}"
    compose stop recv >/dev/null
    if compose logs recv | grep -q "creating session"; then echo "Roc session from the phone: yes"
    else echo "Roc session from the phone: none (is the app sending?)"; fi
    python3 dev/wavstats.py dev/out/from-phone.wav
    ;;
  logs)
    adb -s "$SERIAL" shell run-as com.nickmarcha.bragi cat files/diagnostics.log
    ;;
  down)
    compose down
    for port in "${PHONE_PORTS[@]}"; do adb -s "$SERIAL" emu redir del "udp:$port" >/dev/null 2>&1 || true; done
    ;;
  *)
    sed -n '2,7p' "$0"; exit 1
    ;;
esac
