#!/bin/bash
# Snapshot the PipeWire graph on sagepi, then restart PipeWire, WirePlumber
# and the Bragi peers. Run by bragi-audio-reset@<reason>.service, either from
# the dashboard's "Reset audio" button (bragi-audio-reset.path, reason
# "manual") or nightly (bragi-audio-reset-nightly.timer, reason "nightly").
# See docs/deployment.md.
#
# Added after deck-assistant #108: every sender came out of the headset as
# garbled bursts until the stack was restarted, and nothing from before the
# restart survived to say why. So the snapshot comes first, every time.
#
# No `set -e`: a diagnostic that fails must never stop the restart.
set -uo pipefail

reason="${1:-manual}"
data="${BRAGI_DATA_DIR:-$HOME/.local/share/bragi/data}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
keep=20
tap_seconds=5

# UTC, so app/audio_reset.py can compare it with the request timestamp.
out="$data/incidents/$(date -u +%Y%m%d-%H%M%S)-$reason"
mkdir -p "$out"
echo "snapshot: $out"

{
  date -u
  uptime
  systemctl --user show -p ActiveEnterTimestamp pipewire.service wireplumber.service bragi-peers.service
} > "$out/summary.txt" 2>&1
pw-top -b -n 3 > "$out/pw-top.txt" 2>&1
wpctl status > "$out/wpctl-status.txt" 2>&1
pw-dump > "$out/pw-dump.json" 2>&1
pw-link -l > "$out/pw-links.txt" 2>&1
for pcm in /proc/asound/card*/pcm*/sub0; do
  echo "== $pcm"; cat "$pcm/status" "$pcm/hw_params"
done > "$out/alsa.txt" 2>&1
dmesg 2>&1 | tail -n 300 > "$out/dmesg.txt"
# sagepi keeps its journal in RAM (Storage=volatile), so it survives this
# restart but not a reboot. Copying it here is what keeps it. `sage` reads
# it through the adm group.
journalctl --no-pager --since -2h \
  --user-unit=pipewire.service --user-unit=wireplumber.service \
  --user-unit=pipewire-pulse.service --user-unit=bragi-peers.service \
  > "$out/journal.txt" 2>&1

# Record every stream feeding a headset on its own, before mixing. A sink
# monitor only shows the mix, which can't say which sender is broken.
# `pw-record --target <stream>` doesn't work for this: on a playback stream
# it silently falls back to the default sink's monitor. So each recorder
# starts unlinked and gets linked by hand.
mapfile -t feeds < <(awk '
  /^[^ ]/ { port = $0; next }
  /\|-> alsa_output\./ { split(port, p, ":"); print p[1] }
' "$out/pw-links.txt" | sort -u)

pids=()
for i in "${!feeds[@]}"; do
  pw-record --target 0 -P "{ node.name=bragi-tap-$i node.autoconnect=false }" \
    --rate 48000 --channels 2 --format s16 "$out/tap-${feeds[$i]}.wav" 2>/dev/null &
  pids+=($!)
done
if ((${#pids[@]})); then
  sleep 1
  for i in "${!feeds[@]}"; do
    awk -v node="${feeds[$i]}" '
      /^[^ ]/ { split($0, p, ":"); if (p[1] == node && $0 ~ /:(receive|output|capture|monitor)_/) print $0 }
    ' "$out/pw-links.txt" | sort -u | while read -r port; do
      case "$port" in
        *_FR) pw-link "$port" "bragi-tap-$i:input_FR" ;;
        *) pw-link "$port" "bragi-tap-$i:input_FL" ;;
      esac
    done
  done
  sleep "$tap_seconds"
  kill "${pids[@]}" 2>/dev/null
  wait 2>/dev/null
fi

echo "restarting pipewire"
systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service
sleep 2
# BindsTo=pipewire.service stops it with the daemon but doesn't start it again.
systemctl --user restart bragi-peers.service
sleep 5
pw-top -b -n 2 > "$out/after-pw-top.txt" 2>&1
touch "$out/done"

# Per-second level stats, after the restart so they don't delay it.
# Garbage shows up as near-full-scale peaks, sample jumps over a third of
# full scale, or a zero-crossing rate near 0.5.
python3 -I - "$out" > "$out/levels.txt" 2>&1 <<'PY'
import glob, math, os, struct, sys, wave
for path in sorted(glob.glob(os.path.join(sys.argv[1], 'tap-*.wav'))):
    print('==', os.path.basename(path)[4:-4])
    try:
        w = wave.open(path)
        data = w.readframes(w.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        print('  unreadable:', exc)
        continue
    left = struct.unpack('<%dh' % (len(data) // 2), data)[0::2]
    if not left:
        print('  empty')
    for start in range(0, len(left), 48000):
        b = left[start:start + 48000]
        rms = math.sqrt(sum(x * x for x in b) / len(b))
        zcr = sum((a < 0) != (c < 0) for a, c in zip(b, b[1:])) / len(b)
        jumps = sum(abs(a - c) > 11000 for a, c in zip(b, b[1:]))
        clipped = sum(abs(x) > 32000 for x in b)
        print(f'  {start // 48000}s rms={rms:6.0f} peak={max(map(abs, b)):5d} '
              f'zcr={zcr:.3f} jumps={jumps} clipped={clipped}')
PY
# ~1 MB per stream. Keep the audio only when someone pressed the button
# because something sounded wrong.
[[ "$reason" == manual ]] || rm -f "$out"/tap-*.wav

# Keep the newest $keep snapshots. Names sort by time.
find "$data/incidents" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9][01][0-9][0-3][0-9]-*' \
  | sort | head -n -"$keep" | xargs -r rm -rf --
echo "done"
