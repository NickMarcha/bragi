#!/bin/bash
# Builds a patched libroc.so.0.4 (roc-toolkit v0.4.0 + patches/*.patch) for
# the Bragi Client to ship. The client copies it to ~/.local/lib/bragi/roc/
# and points bragi-roc-link.service at it with LD_LIBRARY_PATH, so the
# pw-cli that hosts the Roc modules loads this copy instead of the distro's.
#
# Why: roc-toolkit 0.4.0's LatencyTuner compares its uint32 stream positions
# with plain < and >=. After 2^32 samples (~24.9h of streaming at 48kHz)
# report_pos_ wraps and the while loop in report_() can never exit, pinning
# PipeWire's data-loop.0 at 100% and killing the link. Upstream master still
# has the bug as of 2026-10-11. See deck-assistant issue #081.
#
# libuv, openfec and speexdsp are built from source and linked in statically
# (--build-3rdparty), so the result only needs libc/libstdc++ at runtime and
# doesn't care which versions the client distro ships.
#
# Usage: build-libroc.sh <output-dir>   (needs scons, a C/C++ toolchain, curl)
set -euo pipefail

ROC_VERSION=0.4.0
ROC_SHA256=60501dfdc8c0de385898dbd1eb8239d93ef962667ddb064ad796b887a41a1a46

out_dir="$(realpath -m "${1:?usage: build-libroc.sh <output-dir>}")"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
trap 'rc=$?; if [[ $rc -eq 0 ]]; then rm -rf "$work"; else echo "build-libroc: failed, work dir kept at $work" >&2; fi' EXIT

# openfec's CMakeLists predates CMake 3.5, and CMake 4 refuses to configure
# it without this. Older CMake ignores the variable.
export CMAKE_POLICY_VERSION_MINIMUM=3.5

curl -sSfL -o "$work/roc.tar.gz" \
  "https://github.com/roc-streaming/roc-toolkit/archive/refs/tags/v${ROC_VERSION}.tar.gz"
echo "${ROC_SHA256}  $work/roc.tar.gz" | sha256sum -c -
tar xzf "$work/roc.tar.gz" -C "$work"
src="$work/roc-toolkit-${ROC_VERSION}"

for p in "$script_dir"/patches/*.patch; do
  patch -d "$src" -p1 --forward < "$p"
done

(cd "$src" && scons -Q -j"$(nproc)" \
  --build-3rdparty=libuv,openfec,speexdsp,ragel \
  --disable-tools --disable-sox --disable-sndfile --disable-openssl \
  --disable-libunwind --disable-alsa --disable-pulseaudio)

lib="$(find "$src/bin" -name "libroc.so.${ROC_VERSION%.*}" -print -quit)"
[[ -n "$lib" ]] || { echo "build-libroc: libroc.so.${ROC_VERSION%.*} not found in $src/bin" >&2; exit 1; }

mkdir -p "$out_dir"
install -m 755 "$lib" "$out_dir/libroc.so.${ROC_VERSION%.*}"
echo "built $out_dir/libroc.so.${ROC_VERSION%.*}"
