#!/usr/bin/env python3
"""The password that guards anything on the board which can change the system.

    python3 dashboard/board_password.py set     # reads the password from stdin
    python3 dashboard/board_password.py check   # reads a candidate from stdin

**The password is never stored.** What is stored is a scrypt hash of it beside a
random salt, in a file outside every git repository, mode 600. A hash cannot be
read back into a password, so a copy of the file is not a copy of the password.

**scrypt, not sha256.** A fast hash is the wrong tool here: the board is on a
public URL, so an attacker who ever obtained the file could try billions of
candidates a second against a fast hash. scrypt is deliberately slow and
memory-hard, which turns billions per second into thousands. The parameters below
are the Python docs' interactive-login figures; they cost about a tenth of a
second per attempt on this box, which is nothing for one login and ruinous for a
dictionary.

**Rate limiting is not optional here and does not live in the hash.** A 13
character password is strong against a human and weak against a machine allowed
to guess without limit, so the server refuses attempts after a handful of failures
and the refusal widens each time. That belongs at the door, and it is why this
module exposes the attempt counters rather than only a yes/no.

**Not in git, and it cannot be.** The file lives under the operator's config
directory beside the settings, which no repository tracks. Rule 9's scan is for
values that reach a repo; the design here is that the value never has one to reach.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import sys
import time
from dataclasses import dataclass

# The Python documentation's own interactive-login parameters for scrypt.
# n is the cost, r the block size, p the parallelism. Raising n doubles both the
# time and the memory, which is the property that makes it worth using.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEY_BYTES = 64
SALT_BYTES = 16

SCHEMA_VERSION = 1

# Where it lives: beside the operator's settings, outside every repository.
PASSWORD_PATH = (
    pathlib.Path.home() / ".config" / "ajit-segment-bots" / "board-password.json"
)


@dataclass(frozen=True)
class PasswordCheck:
    """Whether a candidate matched, said as a state rather than a bare bool."""

    is_correct: bool
    reason: str


def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_KEY_BYTES,
    )


def record_password(password: str, path: pathlib.Path = PASSWORD_PATH) -> pathlib.Path:
    """Store a new password as a salted hash. The password itself is not written.

    Written to a temporary file created with mode 600 from the outset, then
    renamed: a file that is briefly world-readable before a chmod is a file that
    was briefly world-readable.
    """
    if not password:
        raise ValueError("an empty password would admit everyone")
    salt = secrets.token_bytes(SALT_BYTES)
    document = {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": salt.hex(),
        "hash": hash_password(password, salt).hex(),
        "set_at_ns": time.time_ns(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def check_password(candidate: str, path: pathlib.Path = PASSWORD_PATH) -> PasswordCheck:
    """Whether this candidate is the recorded password.

    Compared with `compare_digest`, which takes the same time whether the first
    byte differs or the last. A plain `==` returns sooner on an early mismatch,
    and that difference is measurable over a network -- it lets an attacker
    recover the hash one byte at a time.
    """
    if not path.exists():
        return PasswordCheck(False, "no password has been set on this machine")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        return PasswordCheck(False, f"the password file could not be read: {failure}")
    if document.get("schema_version") != SCHEMA_VERSION:
        return PasswordCheck(False, "the password file was written by another version")

    expected = bytes.fromhex(document["hash"])
    actual = hashlib.scrypt(
        candidate.encode("utf-8"),
        salt=bytes.fromhex(document["salt"]),
        n=int(document["n"]),
        r=int(document["r"]),
        p=int(document["p"]),
        dklen=len(expected),
    )
    if secrets.compare_digest(expected, actual):
        return PasswordCheck(True, "matched")
    return PasswordCheck(False, "did not match")


def is_password_set(path: pathlib.Path = PASSWORD_PATH) -> bool:
    return path.exists()


def read_set_at_ns(path: pathlib.Path = PASSWORD_PATH) -> int | None:
    """When the password was last set, for a board that reports its own security."""
    if not path.exists():
        return None
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("set_at_ns") or 0) or None
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    # Read from stdin, never from argv: an argument is visible in `ps` to every
    # user on the machine and lands in shell history.
    secret = sys.stdin.readline().rstrip("\n")
    if action == "set":
        written = record_password(secret)
        mode = oct(written.stat().st_mode & 0o777)
        print(f"password recorded at {written} (mode {mode}) — the password itself is not in it")
    else:
        verdict = check_password(secret)
        print("correct" if verdict.is_correct else f"refused: {verdict.reason}")
        sys.exit(0 if verdict.is_correct else 1)
