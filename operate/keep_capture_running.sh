#!/usr/bin/env bash
# Keep one venue's trade capture running, and record every time it had to restart.
#
#   operate/keep_capture_running.sh binance-usdm
#
# The capture is the one thing in this project that cannot be caught up on later:
# history accrues only in real time. A crash that nobody notices is therefore not
# an outage, it is a permanent hole in the record. So this restarts the capture
# and, more importantly, writes down that it did -- a tape with a gap and no note
# of the gap is worse than one with a gap that says so (Rule 8).
#
# This is not the governor. The governor forks, places and switches parts, and it
# is a later phase. This is the smallest honest thing that keeps bytes landing
# until that exists, and it should be deleted when it does.
#
# systemd --user would be the right home for this, but this box has no
# passwordless sudo and so no `loginctl enable-linger`: without lingering the
# user manager is killed when the last session ends, which is exactly when this
# most needs to still be running. setsid plus this loop survives a logout; a
# reboot is covered by the crontab @reboot entry that starts it.

set -uo pipefail

VENUE="${1:?usage: keep_capture_running.sh <venue-id>}"
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${HOME}/.local/share/ajit-segment-bots"
LOCK_FILE="${STATE_DIR}/${VENUE}.supervisor.lock"
SUPERVISOR_LOG="${STATE_DIR}/${VENUE}.supervisor.jsonl"
CAPTURE_LOG="${STATE_DIR}/${VENUE}.out"

mkdir -p "${STATE_DIR}"

# One supervisor per venue. Without this, the crontab @reboot line and a manual
# start would both run, and two captures on one venue would write every message
# to the tape twice -- which is worse than not capturing, because a duplicate is
# indistinguishable from a real second print.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "another supervisor already holds ${LOCK_FILE}; exiting" >&2
    exit 0
fi

note() {
    printf '{"observed_at":"%s","venue":"%s","event":%s}\n' \
        "$(date --iso-8601=seconds)" "${VENUE}" "$1" >> "${SUPERVISOR_LOG}"
}

note '"supervisor-started"'
trap 'note "\"supervisor-signalled\""; kill -TERM ${CAPTURE_PID:-0} 2>/dev/null; exit 0' TERM INT

# The wait after a crash. Long enough that a capture failing instantly cannot
# become a connection storm against a venue that bans per IP, short enough that a
# transient fault costs seconds of tape rather than minutes.
RESTART_WAIT_SECONDS=5

while true; do
    started_at=$(date +%s)
    "${PROJECT}/.venv/bin/python" "${PROJECT}/operate/start_trade_capture.py" "${VENUE}" \
        >> "${CAPTURE_LOG}" 2>&1 &
    CAPTURE_PID=$!
    wait "${CAPTURE_PID}"
    exit_code=$?
    ran_for=$(( $(date +%s) - started_at ))
    note "{\"restarting\":true,\"exit_code\":${exit_code},\"ran_for_seconds\":${ran_for}}"
    sleep "${RESTART_WAIT_SECONDS}"
done
