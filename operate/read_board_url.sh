#!/usr/bin/env bash
# The board's current public URL, read from the tunnel's own journal.
#
#     operate/read_board_url.sh
#
# Read rather than remembered. A quick tunnel is issued a fresh random hostname
# every time cloudflared restarts -- on reboot, on a network blip, on any
# restart at all -- so a URL written to a file once would be confidently wrong
# the first time that happened, which is worse than not having it at all.
#
# Only the journal since the *current* start is searched, for the same reason:
# every previous run left its own dead URL in there, and the newest line of an
# older run is still a URL that no longer resolves.
set -euo pipefail

if ! systemctl --user is-active --quiet ajit-board-tunnel.service; then
  echo "NOT RUNNING: ajit-board-tunnel.service is not active" >&2
  echo "  start it with: systemctl --user start ajit-board-tunnel" >&2
  exit 1
fi

started_at="$(systemctl --user show ajit-board-tunnel.service -p ActiveEnterTimestamp --value)"
url="$(journalctl --user -u ajit-board-tunnel.service --since "$started_at" --no-pager 2>/dev/null \
        | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"

if [ -z "$url" ]; then
  echo "NOT MEASURED: the tunnel is running but has not announced a URL yet" >&2
  echo "  it prints one within a few seconds of starting; try again, or read:" >&2
  echo "  journalctl --user -u ajit-board-tunnel -n 40 --no-pager" >&2
  exit 1
fi

echo "$url"
