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

## What survives what

| event | survives? | why |
|---|---|---|
| the capture process crashes | **yes** | `keep_capture_running.sh` restarts it after 5 s and writes down that it did |
| the SSH session ends | **yes** | started with `setsid`, so it is not in the session's process group |
| `SIGKILL` of the capture | **yes, minus the message in flight** | the tape format is built against it, and the writer is unbuffered so records reach the kernel as they are appended |
| **the machine reboots** | **no** | see below |

### Making it survive a reboot — two commands, one of them root

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
whose last entry is old is a capture that is not running.

## What is captured right now

Read it rather than trust this file: `captured_venues`, `captured_symbol_count`
and `symbol_selection_metric` in
`~/.config/ajit-segment-bots/settings/runtime.toml` decide it, and
`docs/settings-schema.md` says what each one costs if it is wrong. As shipped:
both phase 1 venues, the 30 highest-volume symbols on each, trades only.

Candles, the book, and everything else in `docs/superpowers/plans/2026-08-21-market-data-feed.md`
are still to come. A symbol that is not in the top 30 is captured by nobody, and
that is a decision recorded in settings rather than an oversight.
