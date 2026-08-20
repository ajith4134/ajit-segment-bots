# ajit-segment-bots

`/home/anushadudekula71/ajit-segment-bots` is the home directory for this project.
Everything built for it lives inside this directory. Claude Code starts here on
this server by default (see *Startup* below).

## This project is not the trading-system project

It is unrelated to `~/trading-system`, `~/capture`, `~/research`, the AJIT MASTER
PLAN, and the `RL-0xx` rulings. Those govern that project, not this one.

Do not import its design decisions, architecture, naming, venv, or code here
unless explicitly asked. If something from there is genuinely wanted, the user
says so — it is never assumed.

The global rules in `~/.claude/CLAUDE.md` still apply in full: verification
before claiming completion (Rule 0), model selection for subagents (Rule 1),
install rather than skip (Rule 3), the enforcement hooks (Rule 4), research
tooling (Rule 5), interview-spec-plan before creative work (Rule 6), names that
state what the thing does (Rule 7), displays that show measured state (Rule 8),
and everything lives in GitHub (Rule 9).

## Goal

**NOT YET DEFINED.** The user has not given the goal for this project. Interview
for it before designing or building anything — do not infer one from the
directory name.

Nothing below this line has been decided: stack, language, architecture,
dependencies, repository. Each is an open question, not a deferred default.

## Startup

`~/.bash_aliases` defines a `claude` shell function that runs Claude Code from
this directory regardless of where the shell was. To run it somewhere else for
one invocation:

    CLAUDE_KEEP_CWD=1 claude

The function lives in `~/.bash_aliases` rather than `~/.bashrc` because the
`protect-files.sh` PreToolUse hook guards shell profiles, and `~/.bashrc`
already sources that file. It is a shell function rather than a wrapper script
in `~/.local/bin` because Claude Code auto-updates rewrite the symlink there,
which would silently revert the behaviour.

## Status board

    python3 dashboard/build_status_board.py

Regenerates `dashboard/status-board.html` from probes that actually run. Every
tile traces to one. Nothing on it is hand-written, and anything unprobed renders
as `NOT MEASURED` — never as healthy. Re-publish that file to the same Artifact
URL to update the shared link.
