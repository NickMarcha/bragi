#!/bin/bash
# One-time install, on sagepi, of the units that run the dashboard's
# Bragi-managed Roc peers. Bragi itself only writes
# ~/.local/share/bragi/data/peers.conf (app/peers.py); these run it and
# restart it when it changes. See docs/deployment.md.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$UNIT_DIR"
for unit in bragi-peers.service bragi-peers-reload.service bragi-peers.path; do
  install -m 644 "$SCRIPT_DIR/$unit" "$UNIT_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now bragi-peers.path
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

echo
systemctl --user --no-pager status bragi-peers.path bragi-peers.service || true
