# Operating the capture

**The tape is the one thing in this project that cannot be caught up on later.**
Every other part can be built against a tape that already exists; the tape can
only be built by starting it, and an hour not captured is gone permanently. That
is why these scripts exist before the governor does, and why they should be
deleted when it arrives.

Nothing here is a part. `operate/` holds operations entry points that stand in
for two parts that do not exist yet:

| what it stands in for | why it is not that thing |
|---|---|
| `stream-budget-planner` producing `stream-plan` | the plan is built from the symbol catalogue and the adapter's own fit question, in a script, not by a part on the substrate |
| the resource governor forking, placing and switching parts | the part runs in the foreground with a control socket nothing else holds; the switch is a signal |

## Start, stop, look

```bash
# start one venue under a supervisor that restarts it if it dies
setsid nohup bash operate/keep_capture_running.sh binance-usdm >/dev/null 2>&1 &
setsid nohup bash operate/keep_capture_running.sh bybit-linear  >/dev/null 2>&1 &

# what is running
pgrep -af 'operate/start_trade_capture.py'

# stop (SIGTERM runs the close path, which flushes both tape files)
pkill -f keep_capture_running.sh          # stop the supervisor first, or it restarts
for pid in $(pgrep -f start_trade_capture.py); do kill -TERM "$pid"; done

# what it has actually captured
python3 - <<'PY'
import pathlib, sys
sys.path.insert(0, str(pathlib.Path.home() / "ajit-segment-bots"))
from runtime.forkserver_launcher import apply_blas_thread_caps; apply_blas_thread_caps()
from runtime.tape import count_whole_records
root = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
for venue in sorted(root.iterdir()):
    records = sum(count_whole_records(i) for s in venue.iterdir() for i in s.glob("*.index"))
    print(venue.name, records, "records")
PY
```

Everything the capture measured about itself is in
`~/.local/share/ajit-segment-bots/capture-health/<venue>.jsonl` — one line per
health interval, carrying records written, symbols seen, unreadable messages,
and each connection's opens, closes and reconnects. Restarts are in
`<venue>.supervisor.jsonl`. Neither file is the tape: the tape is what the venue
said, these are what we observed about our own reading of it.

## Which side runs — the spine, since 2026-08-24

The tape's normal writer is the **live spine**, not the capture script: the
systemd user unit `ajit-spine` (`operate/ajit-spine.service`) runs
`run_live_spine.py` at boot, restarts it if it dies, and stops it with SIGTERM
so the tape and journals flush. Installed after the reboot of 2026-08-24 04:35
proved the gap: the capture units came back at boot, the hand-started spine did
not, and eleven hours of learning on live prices were lost (the capture kept
the tape).

```bash
systemctl --user status  ajit-spine     # is it up, and since when
systemctl --user restart ajit-spine     # clean stop, flush, start
```

The `ajit-capture@` units below stay installed but **disabled**, as the
fallback for running capture without the spine. One side enabled, never both —
the spine and the capture script refuse to run together, and systemd's
`Conflicts=` makes it agree rather than flap restarts against that refusal:

```bash
systemctl --user disable --now ajit-spine
systemctl --user enable  --now ajit-capture@binance-usdm ajit-capture@bybit-linear
```

## What survives what (the capture fallback)

| event | survives? | why |
|---|---|---|
| the capture process crashes | **yes** | `keep_capture_running.sh` restarts it after 5 s and writes down that it did |
| the SSH session ends | **yes** | started with `setsid`, so it is not in the session's process group |
| `SIGKILL` of the capture | **yes, minus the message in flight** | the tape format is built against it, and the writer is unbuffered so records reach the kernel as they are appended |
| **the machine reboots** | **yes, since 2026-08-22** | the `ajit-capture@` units are installed and lingering is on; they only start if *enabled*, and they are disabled while the spine holds the tape |

### Installing the capture units — two commands, one of them root

There is no cron on this box (`crontab` is not installed and `apt-get` needs a
password), and a systemd **user** service is killed when the last login session
ends unless lingering is enabled, which also needs root. So:

```bash
sudo loginctl enable-linger "$USER"                     # the one root command
mkdir -p ~/.config/systemd/user
cp operate/ajit-capture@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ajit-capture@binance-usdm ajit-capture@bybit-linear
```

Then stop the shell supervisor — two things capturing one venue would write every
message to the tape twice, and a duplicate is indistinguishable from a real
second print:

```bash
pkill -f keep_capture_running.sh
```

Until that is done, **a reboot silently ends the capture.** The supervisor logs
would simply stop, which is why they carry a `supervisor-started` line: a log
whose last entry is old is a capture that is not running. (Done on this box
2026-08-22; the units now sit disabled behind the spine.)

## What is captured right now

Read it rather than trust this file: `captured_venues`, `captured_symbol_count`
and `symbol_selection_metric` in
`~/.config/ajit-segment-bots/settings/runtime.toml` decide it, and
`docs/settings-schema.md` says what each one costs if it is wrong. As shipped:
both phase 1 venues, the 30 highest-volume symbols on each, trades only.

Candles, the book, and everything else in `docs/superpowers/plans/2026-08-21-market-data-feed.md`
are still to come. A symbol that is not in the top 30 is captured by nobody, and
that is a decision recorded in settings rather than an oversight.

## The live board, and its public link

    systemctl --user status ajit-board          # the API and frontend, loopback only
    systemctl --user status ajit-board-tunnel   # the public URL
    operate/read_board_url.sh                   # what that URL is right now

Two units rather than one. `ajit-board` serves
`dashboard/part_health_api.py` on `127.0.0.1:8787` and nothing else, so the
board can be watched over an SSH forward with nothing exposed:

    ssh -L 8787:localhost:8787 <this box>   # then open http://localhost:8787

`ajit-board-tunnel` is what makes it reachable from outside. It runs a
cloudflared **quick tunnel**, which dials *out* to Cloudflare and lets them
proxy back down that connection — the firewall is untouched and port 22 stays
the only thing listening here. Free, and it needs no Cloudflare account.

**The URL changes on every restart.** A quick tunnel is issued a fresh random
hostname each time cloudflared starts: on reboot, on a network blip, on any
restart at all. That is why `read_board_url.sh` reads it out of the unit's
journal instead of a file — a remembered URL would be confidently wrong the
first time the tunnel bounced, which is worse than not having one. A stable URL
needs a *named* tunnel, which needs a Cloudflare account and a browser login
only the operator can do.

**What is exposed, stated rather than assumed.** The URL is unguessable but
public and uncontrolled: anyone holding it can read the board. What they can
read is which parts are running and what their counters say. Every route is a
`GET`, the process only reads the heartbeat table, the blueprint and the built
frontend, it holds no credential, and nothing on it can place an order. To stop
publishing entirely:

    systemctl --user disable --now ajit-board-tunnel

which leaves `ajit-board` running for the SSH-forwarded view.

### Proving it draws

    dashboard/web/verify_live_board_renders.sh https://<the-url>/

Never trust a green `npm run build` for this. The checker mounts the page in a
real Chromium, waits for a **second** poll so a rate actually exists, opens a
block, opens a part, asserts its counters are on screen, and fails on any
console error. It is separate from `verify_board_renders.sh` because that one
waits for `networkidle`, which never fires on a page that polls forever.
