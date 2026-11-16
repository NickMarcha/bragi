#!/bin/bash
# Builds the bragi .deb for the machine it runs on (Debian/Raspberry Pi OS
# trixie, arm64 in CI). Run from the repository root, as root, in a clean
# Debian container: it builds the Python environment in place at
# /opt/bragi/venv, because a venv only works at the path it was made in.
#
#   packaging/build-deb.sh 0.2.0
#
# Leaves dist/bragi_<version>_<arch>.deb. See docs/deployment.md.
set -euo pipefail

version="${1:?usage: packaging/build-deb.sh <version>}"
arch="$(dpkg --print-architecture)"
root="$(pwd)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# The app and its own Python environment. Debian's python3 runs it; the
# packages Debian lacks (webauthn) or has too old come from PyPI, pinned by
# requirements.txt.
rm -rf /opt/bragi
mkdir -p /opt/bragi
cp -r "$root/app" /opt/bragi/app
find /opt/bragi/app -name __pycache__ -prune -exec rm -rf {} +
python3 -m venv /opt/bragi/venv
/opt/bragi/venv/bin/pip install --quiet --no-cache-dir --upgrade pip
/opt/bragi/venv/bin/pip install --quiet --no-cache-dir -r "$root/requirements.txt"
mkdir -p "$stage/opt"
cp -a /opt/bragi "$stage/opt/bragi"

# The user units: Bragi itself and the host units that act on its requests.
units="$stage/usr/lib/systemd/user"
mkdir -p "$units"
install -m 644 "$root/packaging/systemd/bragi.service" "$units/"
for unit in bragi-peers.service bragi-peers-reload.service bragi-peers.path \
            bragi-audio-reset@.service bragi-audio-reset.path bragi-audio-reset-nightly.timer; do
  # The reset script lives in the package, not in ~/.local/bin.
  sed 's|%h/.local/bin/bragi-audio-reset|/usr/lib/bragi/bragi-audio-reset|' \
    "$root/host/systemd/$unit" > "$units/$unit"
  chmod 644 "$units/$unit"
done
install -D -m 755 "$root/host/bragi-audio-reset.sh" "$stage/usr/lib/bragi/bragi-audio-reset"

# The updater: a root service Bragi asks through a request file.
install -D -m 755 "$root/packaging/bragi-update" "$stage/usr/lib/bragi/bragi-update"
install -d "$stage/usr/lib/systemd/system"
install -m 644 "$root"/packaging/systemd/system/bragi-update* "$stage/usr/lib/systemd/system/"
install -D -m 644 "$root/packaging/tmpfiles/bragi.conf" "$stage/usr/lib/tmpfiles.d/bragi.conf"
echo "$version" > "$stage/opt/bragi/VERSION"

# udev: hidraw access for the headset dongle and Bluetooth HID devices.
install -d "$stage/usr/lib/udev/rules.d"
install -m 644 "$root"/host/udev/*.rules "$stage/usr/lib/udev/rules.d/"

# Package metadata and scripts.
install -d "$stage/DEBIAN"
sed -e "s/@VERSION@/$version/" -e "s/@ARCH@/$arch/" \
    -e "s/@SIZE@/$(du -sk "$stage" | cut -f1)/" "$root/packaging/debian/control" > "$stage/DEBIAN/control"
for script in postinst prerm postrm; do
  install -m 755 "$root/packaging/debian/$script" "$stage/DEBIAN/$script"
done

mkdir -p "$root/dist"
dpkg-deb --root-owner-group --build "$stage" "$root/dist/bragi_${version}_${arch}.deb"
echo "built dist/bragi_${version}_${arch}.deb"
