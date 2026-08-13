#!/bin/bash
# One-time, per-machine install of the Bragi Roc link systemd unit. Run by
# hand (not invoked by the Bragi Client GUI app - see client/README.md for
# why: the values roc-link.env needs are machine-specific and can't be
# safely guessed, and a GUI app silently writing systemd units is a bigger
# footgun than one documented manual step).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/.local/bin" "$HOME/.config/systemd/user" "$HOME/.config/bragi-client"

install -m 755 "$SCRIPT_DIR/bragi-roc-link-run.sh" "$HOME/.local/bin/bragi-roc-link-run.sh"
install -m 644 "$SCRIPT_DIR/bragi-roc-link.service" "$HOME/.config/systemd/user/bragi-roc-link.service"

env_file="$HOME/.config/bragi-client/roc-link.env"
if [[ -f "$env_file" ]]; then
  echo "Config already exists at $env_file - leaving it alone."
else
  cp "$SCRIPT_DIR/roc-link.env.example" "$env_file"
  echo "Wrote $env_file from the example template."
  echo "EDIT IT NOW - it has sage-dev's captured values, which are almost"
  echo "certainly wrong for this machine unless this IS sage-dev. Verify"
  echo "against this machine's own PipeWire Roc config before enabling."
fi

systemctl --user daemon-reload

echo
echo "Installed. Next steps:"
echo "  1. Edit $env_file if you haven't already."
echo "  2. Disable any existing static Roc pipewire.conf.d drop-in on this"
echo "     machine and restart pipewire.service --user ONCE, so the old"
echo "     static modules don't double-load alongside this unit."
echo "     (See client/README.md 'Migrating from a static drop-in'.)"
echo "  3. systemctl --user enable --now bragi-roc-link.service"
echo "  4. systemctl --user status bragi-roc-link.service"
