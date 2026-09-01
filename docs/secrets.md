# Secrets — where they live and why not here

**The values are not in this repository and must never be.** This file
explains what was done instead.

## Why no values are in the repo

This project is pushed to GitHub. Rule 9: a push cannot be recalled — deleting a
repository does not recall a clone, a fork, or a crawler's cache. Private is not
a safety property either: private repositories get cloned onto laptops, forked
into other accounts, and made public by a single settings click, at which point
the entire history is exposed, not just the current files.

A credential committed once is a credential that must be rotated. With a real
broker login that means an account that can place orders and move money.

So the split is: **the repo carries the inventory, the machine carries the
values.**

## The store — `~/.config/ajit-segment-bots/`

Created 2026-09-01, when the project's goal converted from crypto to Indian
stock trading (`docs/goal.md`) and its credential needs stopped being the
crypto exchange keys the project previously borrowed from `~/.config/trading/`.
Own store now, same age identity:

| What | Path | Notes |
|---|---|---|
| age private key | `~/.config/sops/age/keys.txt` | same identity as the old crypto store. Decrypts both. Not in any repo. |
| encrypted store | `~/.config/ajit-segment-bots/secrets.enc.yaml` | mode 600, values encrypted, key names in clear |
| sops recipients | `~/.config/ajit-segment-bots/.sops.yaml` | public key only, safe by design. Broader `encrypted_regex` than the crypto store's — see the file's own header comment for why. |

**Crypto's old store (`~/.config/trading/`) is untouched and no longer read by
this project.** It held `binance`/`coinbase`/`bybit` API keys for the retired
crypto build; git history (`git log -p docs/secrets.md`) has the prior version
of this file if that's ever needed again.

## What's in the store now

Read from the encrypted file's key names only. **No value has ever been
decrypted, printed, or copied into this conversation or any transcript.**

| group | fields |
|---|---|
| `upstox_api` | `client_id`, `client_secret`, `redirect_uri` |
| `upstox_login` | `username`, `password`, `pin_code`, `totp_secret` |

All still hold the shipped `PLACEHOLDER_` values — real credentials have not
been entered. `upstox_login` exists because the daily-token-refresh design
(`docs/superpowers/specs/2026-09-01-upstox-adapter-design.md`) depends on
`upstox-totp` for fully unattended auth, which needs the account's login
credentials, not just the OAuth app's API key pair — a full account
takeover if leaked, not merely API-scoped access, which is why the whole
group is encrypted (`password`, `pin_code`, `totp_secret`, `username`) rather
than just the traditional `api_key`/`api_secret` pair.

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

    sops ~/.config/ajit-segment-bots/secrets.enc.yaml

Opens the decrypted YAML in an editor and re-encrypts on save. Current schema:

    upstox_api:
      client_id: ...
      client_secret: ...
      redirect_uri: ...
    upstox_login:
      username: ...
      password: ...
      pin_code: ...
      totp_secret: ...

Each of the six brokers in `docs/goal.md` §6 gets its own `<broker>_api` (and
`<broker>_login` where a broker needs auto-login credentials, not every one
will) group as its adapter is built — same shape, added when it's earned,
never all six pre-created against brokers with no adapter yet.
