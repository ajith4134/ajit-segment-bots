"""secrets_reader: never a real sops call in a test (RL-063 has no bearing
here -- there is no real data to be honest about, only real credentials to
never touch). `run` is injected, matching broker_token_refresh_scheduler.py's
own generate_token injection pattern, so this is fully testable with no
secrets store, no age key and no sops binary present."""

import pathlib

from runtime.secrets_reader import PLACEHOLDER_PREFIX, read_upstox_env


class FakeResult:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def a_run(stdout, returncode=0):
    def run(*args, **kwargs):
        return FakeResult(returncode, stdout)
    return run


REAL_YAML = """
upstox_api:
  client_id: "11111111-2222-3333-4444-555555555555"
  client_secret: "realsecret"
  redirect_uri: "http://127.0.0.1/upstox/callback"
upstox_login:
  username: "AB1234"
  password: "hunter2"
  pin_code: "1234"
  totp_secret: "REALSECRETBASE32"
"""


def test_a_missing_file_returns_empty_not_raises(tmp_path):
    missing = tmp_path / "does-not-exist.enc.yaml"
    assert read_upstox_env(missing, run=a_run("irrelevant")) == {}


def test_a_failed_decrypt_returns_empty_not_raises(tmp_path):
    path = tmp_path / "secrets.enc.yaml"
    path.write_text("anything")
    assert read_upstox_env(path, run=a_run("", returncode=1)) == {}


def test_real_values_map_to_the_upstox_totp_env_names(tmp_path):
    path = tmp_path / "secrets.enc.yaml"
    path.write_text("anything")
    env = read_upstox_env(path, run=a_run(REAL_YAML))
    assert env == {
        "UPSTOX_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
        "UPSTOX_CLIENT_SECRET": "realsecret",
        "UPSTOX_REDIRECT_URI": "http://127.0.0.1/upstox/callback",
        "UPSTOX_USERNAME": "AB1234",
        "UPSTOX_PASSWORD": "hunter2",
        "UPSTOX_PIN_CODE": "1234",
        "UPSTOX_TOTP_SECRET": "REALSECRETBASE32",
    }


def test_placeholder_values_are_never_exported(tmp_path):
    """The store ships with PLACEHOLDER_ dummy values (docs/secrets.md) --
    exporting one as a real credential would send upstox-totp a string that
    looks configured and fails in a confusing venue-side way instead of the
    honest 'nothing configured yet'."""
    path = tmp_path / "secrets.enc.yaml"
    path.write_text("anything")
    yaml_text = """
upstox_api:
  client_id: "PLACEHOLDER_CLIENT_ID"
  client_secret: "PLACEHOLDER_CLIENT_SECRET"
  redirect_uri: "PLACEHOLDER_REDIRECT_URI"
upstox_login:
  username: "PLACEHOLDER_USERNAME"
  password: "PLACEHOLDER_PASSWORD"
  pin_code: "PLACEHOLDER_PIN_CODE"
  totp_secret: "PLACEHOLDER_TOTP_SECRET"
"""
    env = read_upstox_env(path, run=a_run(yaml_text))
    assert env == {}


def test_a_partially_filled_store_exports_only_the_real_fields(tmp_path):
    path = tmp_path / "secrets.enc.yaml"
    path.write_text("anything")
    yaml_text = """
upstox_api:
  client_id: "11111111-2222-3333-4444-555555555555"
  client_secret: "PLACEHOLDER_CLIENT_SECRET"
  redirect_uri: "http://127.0.0.1/upstox/callback"
upstox_login:
  username: "PLACEHOLDER_USERNAME"
  password: "PLACEHOLDER_PASSWORD"
  pin_code: "PLACEHOLDER_PIN_CODE"
  totp_secret: "PLACEHOLDER_TOTP_SECRET"
"""
    env = read_upstox_env(path, run=a_run(yaml_text))
    assert env == {
        "UPSTOX_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
        "UPSTOX_REDIRECT_URI": "http://127.0.0.1/upstox/callback",
    }


def test_the_placeholder_prefix_matches_the_real_store_s_own_convention():
    assert PLACEHOLDER_PREFIX == "PLACEHOLDER_"


def test_a_missing_sops_binary_returns_empty_not_raises(tmp_path):
    """Found the hard way 2026-09-02: a systemd --user service's own PATH
    does not include ~/.local/bin, where sops actually lives on this box --
    subprocess.run(["sops", ...]) raised FileNotFoundError, uncaught, the
    first time this ran under ajit-spine.service."""
    path = tmp_path / "secrets.enc.yaml"
    path.write_text("anything")

    def raises_file_not_found(*args, **kwargs):
        raise FileNotFoundError("sops: no such file or directory")

    assert read_upstox_env(path, run=raises_file_not_found) == {}
