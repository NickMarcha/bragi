#!/bin/bash
# One-time install, on the Bragi host, of the units that run the dashboard's
# Bragi-managed Roc peers and its "Reset audio" button. Safe to re-run.
# Bragi itself only writes ~/.local/share/bragi/data/peers.conf
# (app/peers.py) and data/reset-request (app/audio_reset.py); these units
# act on them. See docs/deployment.md.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$UNIT_DIR"
for unit in bragi-peers.service bragi-peers-reload.service bragi-peers.path \
            bragi-audio-reset@.service bragi-audio-reset.path bragi-audio-reset-nightly.timer; do
  install -m 644 "$SCRIPT_DIR/$unit" "$UNIT_DIR/$unit"
done
# The dashboard's "Reset audio" button and the nightly reset.
install -D -m 755 "$SCRIPT_DIR/../bragi-audio-reset.sh" "$HOME/.local/bin/bragi-audio-reset"

systemctl --user daemon-reload
systemctl --user enable --now bragi-peers.path
systemctl --user enable --now bragi-audio-reset.path bragi-audio-reset-nightly.timer
# Picks up a changed schedule on a re-run; enable --now leaves a running timer alone.
systemctl --user restart bragi-audio-reset-nightly.timer
systemctl --user enable bragi-peers.service
if [[ -f "$HOME/.local/share/bragi/data/peers.conf" ]]; then
  systemctl --user restart bragi-peers.service
fi

old="$HOME/.config/pipewire/pipewire.conf.d/70-bragi-peers.conf"
if [[ -f "$old" ]]; then
  echo
  echo "NOTE: $old still exists. Bragi no longer writes it, and the main"
  echo "daemon would load it alongside bragi-peers.service on its next"
  echo "restart. Check it's empty or obsolete, then remove it."
fi

# docker-compose.yml's host-specific values, detected here so nobody has to
# look them up. An existing .env is left alone.
env_file="$SCRIPT_DIR/../../.env"
mkdir -p "$HOME/.local/share/bragi/data"
if [[ ! -f "$env_file" ]]; then
  {
    echo "# Written by host/systemd/install.sh from this host. See .env.example."
    echo "BRAGI_UID=$(id -u)"
    echo "BRAGI_GID=$(id -g)"
    input_gid=$(getent group input | cut -d: -f3 || true)
    if [[ -n "$input_gid" ]]; then
      echo "INPUT_GID=$input_gid"
    else
      echo "# No input group on this host: the headset volume knob won't work."
    fi
    echo "BRAGI_DATA=$HOME/.local/share/bragi/data"
  } > "$env_file"
  echo "Wrote $env_file:"
  sed 's/^/  /' "$env_file"
fi

echo
systemctl --user --no-pager status bragi-peers.path bragi-peers.service bragi-audio-reset.path || true
systemctl --user --no-pager list-timers bragi-audio-reset-nightly.timer || true
