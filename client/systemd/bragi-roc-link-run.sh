#!/bin/bash
# ExecStart for bragi-roc-link.service. Loads the two Roc PipeWire modules
# (sink: sagepi headset -> local playback; source: local mic -> sagepi) into
# a *persistent* pw-cli session and then blocks for the unit's whole
# lifetime - see the .service file's comment for why this can't be a
# oneshot script that loads and exits.
#
# Required env vars (from ~/.config/bragi-client/roc-link.env via
# EnvironmentFile=): SAGEPI_TAILSCALE_IP, REMOTE_SOURCE_PORT,
# REMOTE_REPAIR_PORT, REMOTE_CONTROL_PORT, LOCAL_SINK_NAME,
# LOCAL_SOURCE_PORT, LOCAL_REPAIR_PORT, LOCAL_CONTROL_PORT,
# LOCAL_SOURCE_NAME, SESS_LATENCY_MSEC, ROC_MODULE_DIR.

set -euo pipefail

: "${SAGEPI_TAILSCALE_IP:?missing}" "${REMOTE_SOURCE_PORT:?missing}" "${REMOTE_REPAIR_PORT:?missing}" \
  "${REMOTE_CONTROL_PORT:?missing}" "${LOCAL_SINK_NAME:?missing}" "${LOCAL_SOURCE_PORT:?missing}" \
  "${LOCAL_REPAIR_PORT:?missing}" "${LOCAL_CONTROL_PORT:?missing}" "${LOCAL_SOURCE_NAME:?missing}" \
  "${SESS_LATENCY_MSEC:?missing}" "${ROC_MODULE_DIR:?missing}"

# pw-cli loads the module into ITS OWN process (see .service comment) - it
# needs the same module dir the daemon uses to find the locally-built .so,
# pw-cli does its own dlopen for this, independent of whatever the daemon
# already has loaded. ROC_MODULE_DIR is NOT hardcoded - the build directory
# name differs per machine (sage-dev: ~/.local/lib/pipewire-0.3-roc,
# sagedeck: ~/.local/lib/pipewire-0.3-roc-test - found by checking sagedeck
# directly, don't assume they match).
export PIPEWIRE_MODULE_DIR="${ROC_MODULE_DIR}:/usr/lib/pipewire-0.3"

sink_args=$(printf '{"remote.ip":"%s","remote.source.port":%s,"remote.repair.port":%s,"remote.control.port":%s,"fec.code":"disable","sink.name":"%s","sink.props":{"node.name":"%s","node.description":"sagepi headset (Roc, via Bragi Client)"}}' \
  "$SAGEPI_TAILSCALE_IP" "$REMOTE_SOURCE_PORT" "$REMOTE_REPAIR_PORT" "$REMOTE_CONTROL_PORT" "$LOCAL_SINK_NAME" "$LOCAL_SINK_NAME")

source_args=$(printf '{"local.ip":"0.0.0.0","local.source.port":%s,"local.repair.port":%s,"local.control.port":%s,"fec.code":"disable","sess.latency.msec":%s,"source.name":"%s","source.props":{"node.name":"%s","node.description":"sagepi mic (Roc, via Bragi Client)","media.class":"Audio/Source"}}' \
  "$LOCAL_SOURCE_PORT" "$LOCAL_REPAIR_PORT" "$LOCAL_CONTROL_PORT" "$SESS_LATENCY_MSEC" "$LOCAL_SOURCE_NAME" "$LOCAL_SOURCE_NAME")

fifo=$(mktemp -u "${XDG_RUNTIME_DIR:-/tmp}/bragi-roc-link.XXXXXX.fifo")
pwcli_log=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/bragi-roc-link.XXXXXX.log")
mkfifo "$fifo"

cleanup() {
  rm -f "$fifo" "$pwcli_log"
}
trap cleanup EXIT

# pw-cli reads commands from this fifo. We hold the write end open (fd 9,
# below) for our own process's whole lifetime, so pw-cli's stdin never sees
# EOF and it never quits on its own - that's what keeps the loaded modules
# (and their nodes) alive. See client/README.md for how this was confirmed.
pw-cli < "$fifo" > "$pwcli_log" 2>&1 &
pwcli_pid=$!

exec 9> "$fifo"
printf 'load-module libpipewire-module-roc-sink %s\n' "$sink_args" >&9
printf 'load-module libpipewire-module-roc-source %s\n' "$source_args" >&9

# Give pw-cli a moment to process both commands, then check its output for
# a real failure (e.g. bad args, port already bound) before reporting
# ready - a successful `systemctl start` should mean both modules actually
# loaded, not just that pw-cli itself launched.
#
# NOTE: pw-cli prints harmless `Error: "unsupported type ..."` lines during
# its own startup registry sync (SecurityContext/Profiler interfaces it
# doesn't know how to decode) - matching on bare `^Error:` false-positives
# on those and kills an otherwise-working link (confirmed while testing
# this script). "Could not load module" is pw-cli's actual load-module
# failure message.
sleep 1.5
if grep -q 'Could not load module' "$pwcli_log"; then
  echo "bragi-roc-link: module load failed:" >&2
  cat "$pwcli_log" >&2
  kill "$pwcli_pid" 2>/dev/null || true
  wait "$pwcli_pid" 2>/dev/null || true
  exit 1
fi

systemd-notify --ready --status="Roc link up (sink=${LOCAL_SINK_NAME}, source=${LOCAL_SOURCE_NAME})" || true

# pw-cli exits nonzero when killed by SIGTERM, which - if left to propagate
# as this script's own exit code - makes systemd report the unit as
# "failed" after a perfectly normal `systemctl stop` (confirmed while
# testing this script: `is-active` said "failed", not "inactive", even
# though teardown itself worked fine). A deliberate stop must always exit
# 0; only an unrequested pw-cli death (a real crash) should count as a
# failure and trigger Restart=on-failure.
stopping=0
terminate() {
  stopping=1
  kill -TERM "$pwcli_pid" 2>/dev/null || true
}
trap terminate TERM INT

# `set -e` would otherwise abort the script the instant `wait` returns
# pw-cli's nonzero killed-by-signal status, skipping the $stopping check
# below entirely and propagating that nonzero code as our own exit status
# (confirmed while testing this script - that's what caused the
# is-active=failed-after-stop bug above, it wasn't really about `stopping`
# at all).
set +e
wait "$pwcli_pid"
wait_status=$?
set -e

if [[ "$stopping" -eq 1 ]]; then
  exit 0
fi
exit "$wait_status"
