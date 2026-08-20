# Secrets — where they live and why not here

The user asked for the private key and the API/secret key list from the old
project to be saved in this project. **The values are not in this repository and
must never be**, and this file explains what was done instead.

## Why no values are in the repo

This project is pushed to GitHub. Rule 9: a push cannot be recalled — deleting a
repository does not recall a clone, a fork, or a crawler's cache. Private is not
a safety property either: private repositories get cloned onto laptops, forked
into other accounts, and made public by a single settings click, at which point
the entire history is exposed, not just the current files.

A credential committed once is a credential that must be rotated. With live
exchange keys that means an account that can move money.

So the split is: **the repo carries the inventory, the machine carries the
values.**

## What already exists on this server, and where

The old project already stored these correctly — encrypted at rest with sops+age,
outside every git repository, at user level. Nothing needed moving. This project
reads the same store.

| What | Path | Notes |
|---|---|---|
| age private key | `~/.config/sops/age/keys.txt` | this is "the private key". It decrypts the store. Not in any repo. |
| encrypted store | `~/.config/trading/secrets.enc.yaml` | mode 600, values encrypted, key names in clear |
| sops recipients | `~/.config/trading/.sops.yaml` | public keys only, safe by design |

## The venues currently in the store

Read from the encrypted file's key names. **No values were decrypted, printed, or
copied at any point.**

| venue | fields |
|---|---|
| `binance` | `api_key`, `api_secret` |
| `coinbase` | `api_key`, `api_secret` |
| `bybit` | `api_key`, `api_secret` |

Whether each holds a real key or still the shipped `PLACEHOLDER_` value is **not
known** — checking would require decrypting, which was not done. The old
project's reader rejects placeholders by prefix, and this project will do the
same.

## Rules this project inherits

Both come from the old project's reader, and both are worth keeping:

1. **Never decrypt to a file.** `sops -d > secrets.env` leaves plaintext on disk
   that outlives the command, and unattended nothing notices when the cleanup
   silently fails. Decrypt to a pipe; the plaintext exists only as a string in
   the running process.
2. **A placeholder is not a credential.** A store ships with well-formed dummy
   values so the signing path can be tested before a real key exists. Signing
   with one produces a confusing venue error instead of an obvious local one, so
   they are refused by name.

To those, this project adds:

3. **No secret is ever logged, printed, or put in a repr.** The credential type
   overrides `__repr__`, because the default would put the secret into any
   traceback — and tracebacks get pasted into issues and chat.
4. **One store, not a copy.** The same key is not duplicated into a second file.
   Two copies means a rotation that updates one and misses the other, and the
   missed one keeps working until it is the only thing an attacker still has.

## Adding or rotating a key

    sops ~/.config/trading/secrets.enc.yaml

Opens the decrypted YAML in an editor and re-encrypts on save. The schema is:

    <venue>:
      api_key: ...
      api_secret: ...

## If a separate store is wanted for this project

Say so and it is created at `~/.config/ajit-segment-bots/secrets.enc.yaml`,
encrypted to the same age key. It was **not** done by default, because these are
the same real exchange accounts and rule 4 above applies.
