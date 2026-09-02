"""secrets_reader: bridges the operator's sops+age encrypted credential store
(docs/secrets.md) into the environment variable names each broker's own
auto-login library expects to find them in -- upstox-totp reads UPSTOX_*.

**Never decrypts to a file** (docs/secrets.md rule 1). `sops -d` is run with
its stdout captured to a pipe and parsed in this process only; nothing here
prints, logs, or writes a value anywhere. `run` is injected so this is fully
testable with no secrets store, no age key and no sops binary present --
tests/runtime/test_secrets_reader.py never touches a real credential.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import yaml

SECRETS_PATH = pathlib.Path.home() / ".config" / "ajit-segment-bots" / "secrets.enc.yaml"

# A placeholder is not a credential (docs/secrets.md rule 2, inherited from the
# crypto build's own store). Exporting one as a real value would send
# upstox-totp a string that looks configured and fails with a confusing
# venue-side error instead of the honest "nothing configured yet".
PLACEHOLDER_PREFIX = "PLACEHOLDER_"

UPSTOX_ENV_BY_FIELD = {
    ("upstox_api", "client_id"): "UPSTOX_CLIENT_ID",
    ("upstox_api", "client_secret"): "UPSTOX_CLIENT_SECRET",
    ("upstox_api", "redirect_uri"): "UPSTOX_REDIRECT_URI",
    ("upstox_login", "username"): "UPSTOX_USERNAME",
    ("upstox_login", "password"): "UPSTOX_PASSWORD",
    ("upstox_login", "pin_code"): "UPSTOX_PIN_CODE",
    ("upstox_login", "totp_secret"): "UPSTOX_TOTP_SECRET",
}


def _find_sops() -> str | None:
    """`sops` lives in ~/.local/bin (Rule 3: user-space, no root) on this
    box, which a systemd --user service's own PATH does not include -- found
    the hard way, 2026-09-02: the live spine's own subprocess.run(["sops",
    ...]) raised FileNotFoundError, uncaught, the first time this ran under
    ajit-spine.service rather than an interactive shell. Search PATH first
    (a user's own shell, CI, anywhere it is already found normally), then
    that known install location, rather than hardcoding either alone."""
    found = shutil.which("sops")
    if found:
        return found
    local_bin = pathlib.Path.home() / ".local" / "bin" / "sops"
    return str(local_bin) if local_bin.exists() else None


def read_upstox_env(
    path: pathlib.Path = SECRETS_PATH, run=subprocess.run
) -> dict[str, str]:
    """Decrypts the store and returns real (non-placeholder) UPSTOX_* values.

    A missing file, a missing sops binary, a failed decrypt, or every field
    still a placeholder all return an empty dict rather than raising: no
    secrets configured yet is an ordinary startup condition (docs/
    secrets.md), not a reason to refuse starting the parts that don't need a
    broker login at all.
    """
    if not path.exists():
        return {}
    sops = _find_sops()
    if sops is None:
        return {}
    try:
        result = run([sops, "-d", str(path)], capture_output=True, text=True)
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    document = yaml.safe_load(result.stdout) or {}
    env: dict[str, str] = {}
    for (group, field), env_name in UPSTOX_ENV_BY_FIELD.items():
        value = document.get(group, {}).get(field)
        if isinstance(value, str) and value and not value.startswith(PLACEHOLDER_PREFIX):
            env[env_name] = value
    return env


__all__ = ["PLACEHOLDER_PREFIX", "SECRETS_PATH", "UPSTOX_ENV_BY_FIELD", "read_upstox_env"]
