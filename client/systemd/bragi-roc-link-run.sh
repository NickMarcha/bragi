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

# audio.rate 48000 on both node props keeps the whole local graph at one
# rate: without it the Roc modules come up at their 44100 default and, on a
# machine whose PipeWire clock isn't pinned (unlike sagepi, which has
# clock.allowed-rates = [ 48000 ]), the 44100 Roc sink tries to drag the
# whole graph to 44100. With EasyEffects in the path that rate switch
# wedged its output stage and playback to sagepi silently stopped. See
# issue #080. Also pin the clock via 10-clock-rate.conf as a backstop.
# log.level "INFO" makes both modules' underlying libroc session (not just
# the PipeWire node) report connect/disconnect/endpoint state through
# pw_log - see issue #081, where a Roc sink/source silently lost its UDP
# sockets for a day with nothing in any log to explain why. Module-side
# props accept "NONE"/"ERROR"/"INFO"/"DEBUG"/"TRACE"
# (src/modules/module-roc/common.h:pw_roc_parse_log_level).
sink_args=$(printf '{"remote.ip":"%s","remote.source.port":%s,"remote.repair.port":%s,"remote.control.port":%s,"fec.code":"disable","sink.name":"%s","sink.props":{"node.name":"%s","node.description":"sagepi headset (Roc, via Bragi Client)","audio.rate":48000,"log.level":"INFO"}}' \
  "$SAGEPI_TAILSCALE_IP" "$REMOTE_SOURCE_PORT" "$REMOTE_REPAIR_PORT" "$REMOTE_CONTROL_PORT" "$LOCAL_SINK_NAME" "$LOCAL_SINK_NAME")

source_args=$(printf '{"local.ip":"0.0.0.0","local.source.port":%s,"local.repair.port":%s,"local.control.port":%s,"fec.code":"disable","sess.latency.msec":%s,"source.name":"%s","source.props":{"node.name":"%s","node.description":"sagepi mic (Roc, via Bragi Client)","media.class":"Audio/Source","audio.rate":48000,"log.level":"INFO"}}' \
  "$LOCAL_SOURCE_PORT" "$LOCAL_REPAIR_PORT" "$LOCAL_CONTROL_PORT" "$SESS_LATENCY_MSEC" "$LOCAL_SOURCE_NAME" "$LOCAL_SOURCE_NAME")

fifo=$(mktemp -u "${XDG_RUNTIME_DIR:-/tmp}/bragi-roc-link.XXXXXX.fifo")
pwcli_log=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/bragi-roc-link.XXXXXX.log")
mkfifo "$fifo"

trim_pid=""
cleanup() {
  # set +e first: under `set -e`, kill failing (trim_pid already dead, e.g.
  # from systemd's cgroup-wide SIGTERM racing us here) aborts the rest of
  # this trap AND overwrites the script's real exit status with the kill's
  # failure code - discovered because it silently turned every graceful
  # `systemctl stop` into a reported "failed" unit once this trim-loop
  # cleanup was added (issue #081). Confirmed in isolation: a failing
  # command as the last thing an EXIT trap runs replaces `exit 0`'s status
  # with its own, `set +e` here is what stops that.
  set +e
  rm -f "$fifo" "$pwcli_log"
  [[ -n "$trim_pid" ]] && kill "$trim_pid" 2>/dev/null
}
trap cleanup EXIT

# pw-cli reads commands from this fifo. We hold the write end open (fd 9,
# below) for our own process's whole lifetime, so pw-cli's stdin never sees
# EOF and it never quits on its own - that's what keeps the loaded modules
# (and their nodes) alive. See client/README.md for how this was confirmed.
#
# pw-cli's raw output used to go ONLY to $pwcli_log, a tmpfile in
# /run/user nobody ever looked at - `journalctl --user -u
# bragi-roc-link.service` showed nothing at all, even the run where a Roc
# socket silently died (#081). pw-cli mirrors the *entire* PipeWire
# registry (every client connect/disconnect bus-wide) into this stream, so
# a raw copy grows tens of MB a day of pure noise and drowns any real line
# in it. Fix: keep the raw copy in $pwcli_log (capped below, it's tmpfs -
# RAM - so unbounded growth is a real cost, not just clutter) via process
# substitution so pw-cli's own PID is still what `$!` captures (needed for
# the kill/wait logic further down), and also emit a filtered copy - registry
# churn stripped - to this script's own stdout, which the unit's Type=notify
# stdout/stderr capture puts straight into the journal.
#
# journald turned out not to be a safe place to rely on for this: CachyOS
# caps the whole system+user journal at 50M (SystemMaxUse, distro default),
# and ordinary desktop noise alone - Steam, Discord, browser, whatever else
# is running - burns through that in well under a day, so by the time
# anyone goes looking after a days-later recurrence the entries are already
# rotated out. #081 originally read that as a bug in this logging (a
# synergy.service crash loop eating the budget); disabling that helped but
# didn't fix the underlying problem, the shared budget is just too small
# regardless of what else is or isn't flapping. So the filtered copy also
# goes to a persistent on-disk file outside journald's control, appended
# (not truncated) across restarts, since a restart-to-fix is often exactly
# what happens right after the interesting failure.
persistent_log_dir="${XDG_STATE_HOME:-$HOME/.local/state}/bragi"
mkdir -p "$persistent_log_dir"
persistent_log="$persistent_log_dir/roc-link.log"

pw-cli < "$fifo" > >(tee -a "$pwcli_log" | grep --line-buffered -viE '^remote [0-9]+ |^[[:space:]]+[A-Za-z0-9_.]+ = ' | tee -a "$persistent_log") 2>&1 &
pwcli_pid=$!

# Trim $pwcli_log (raw, tmpfs) back down whenever it crosses 10MB, and
# $persistent_log (filtered, on disk, meant to survive across restarts and
# journald rotation) whenever it crosses 20MB - a long-lived link would
# otherwise let either grow without bound.
( while true; do
    sleep 300
    if [[ -f "$pwcli_log" ]] && (( $(stat -c%s "$pwcli_log" 2>/dev/null || echo 0) > 10485760 )); then
      tail -c 2097152 "$pwcli_log" > "$pwcli_log.trimmed" 2>/dev/null && mv "$pwcli_log.trimmed" "$pwcli_log"
    fi
    if [[ -f "$persistent_log" ]] && (( $(stat -c%s "$persistent_log" 2>/dev/null || echo 0) > 20971520 )); then
      tail -c 5242880 "$persistent_log" > "$persistent_log.trimmed" 2>/dev/null && mv "$persistent_log.trimmed" "$persistent_log"
    fi
  done ) &
trim_pid=$!

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
