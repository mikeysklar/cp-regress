# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""The settings.toml scenario fixtures (test/fixtures/secrets.py, after Brent's
hil/firmware/conftest.py:252-366 @ cf64e03) run for real, with no board and no .env.

base_secrets is overridden here with dummy values on farm.toml's first allowed SSID, so
secrets_path renders every scenario in scenarios.toml through the same fixture chain a board
test uses, and the prod guard is checked to stop the run where it must. The verdict and guard
tables in test/hardware/settings_scenarios_test.py check the same files with plain function
calls; this checks the fixtures that hand them out.
"""
import os
import types

import pytest

from fixtures import secrets as fs
from fixtures.secrets import (AIO_KEY, AIO_NAME, DEFAULT_SECRETS_DIR, NO_SECRETS_FILE, PASSWORD,
                              PLACEHOLDER, SECRETS_SUFFIX, SSID, WEB_API_PASSWORD, load_scenarios,
                              scenario_guard, settings_toml, tomllib)

pytestmark = pytest.mark.smoke

SCENARIOS = load_scenarios(DEFAULT_SECRETS_DIR)
DUMMY_PASSWORD = "cpr-selftest-pass-0123"
BROKER = "192.168.0.76"


@pytest.fixture(scope="session")
def base_secrets(pytestconfig):
    """Overrides fixtures.secrets.base_secrets: no broker fixture, no .env."""
    allowed = pytestconfig.cpr_cfg.get("network", {}).get("allowed_ssids", [])
    assert allowed, "farm.toml [network] allowed_ssids is empty: the prod guard refuses all"
    return {SSID: allowed[0], PASSWORD: DUMMY_PASSWORD,
            "CIRCUITPY_WEB_API_PASSWORD": WEB_API_PASSWORD,
            "CPR_BROKER_HOST": BROKER, "CPR_BROKER_PORT": 1884}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """The developer's environment must not decide these tests: drop anything the prod guard
    would refuse (Adafruit IO names and keys) and the scenario dir override."""
    for name, value in list(os.environ.items()):
        if AIO_NAME.search(name) or AIO_KEY.match(value or ""):
            monkeypatch.delenv(name)
    monkeypatch.delenv(fs.SECRETS_DIR_ENV, raising=False)


@pytest.mark.parametrize("secrets_name", list(SCENARIOS), indirect=True)
def test_every_scenario_builds(request, secrets_name, tmp_path, good_secrets):
    """Every scenario renders through secrets_path into tmp_path, filled, guard-clean, and
    with the TOML verdict scenarios.toml gives it. not_ported ones skip, as on the farm."""
    entry = SCENARIOS[secrets_name]
    path = request.getfixturevalue("secrets_path")
    if secrets_name == NO_SECRETS_FILE:
        assert path is None
        return
    assert path.parent == tmp_path and path.name == secrets_name + SECRETS_SUFFIX
    data = path.read_bytes()
    assert not PLACEHOLDER.search(data), "unfilled placeholder"
    assert scenario_guard(request.config.cpr_cfg, {}, data, DUMMY_PASSWORD) == []
    try:
        tomllib.loads(data.decode("utf-8"))
        verdict = "valid"
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        verdict = "invalid"
    assert verdict == entry["spec"], entry["spec_ref"]
    if entry["needs_creds"]:  # it carries the farm SSID or password, so it needs .env
        assert any(good_secrets[k].encode() in data for k in (SSID, PASSWORD))


def test_default_is_the_good_scenario(request):
    """No parametrization: Brent's default (conftest.py:345 @ cf64e03) is the good secrets."""
    assert request.getfixturevalue("secrets_name") == fs.GOOD_SECRETS


@pytest.mark.parametrize("secrets_name", ["wrong_password"], indirect=True)
def test_creds_scenarios_skip_without_wifi(request, monkeypatch, secrets_name, good_secrets):
    """Without CPR_WIFI_SSID / CPR_WIFI_PASSWORD the scenarios that need them skip, rather
    than putting a half-filled file on a board."""
    assert SCENARIOS[secrets_name]["needs_creds"]
    monkeypatch.delitem(good_secrets, PASSWORD)
    with pytest.raises(pytest.skip.Exception, match="CPR_WIFI_PASSWORD not set"):
        request.getfixturevalue("secrets_path")
    gen = fs.build_generated({SSID: fs.PLACEHOLDER_SSID}, good_secrets)
    assert secrets_name not in gen and fs.GOOD_SECRETS in gen


# Files the guard must stop, whatever the scenario is called.
BAD = {
    "public_broker": lambda g: settings_toml(dict(g, CPR_BROKER_HOST="52.1.2.3")).encode(),
    "aio_host": lambda g: b'CPR_BROKER_HOST = "io.adafruit.com"\n',
    "aio_key_name": lambda g: settings_toml(dict(g, ADAFRUIT_AIO_KEY="x")).encode(),
    "aio_key_value": lambda g: settings_toml(dict(g, CPR_X="aio_" + "a" * 28)).encode(),
    "password_with_foreign_ssid": lambda g: settings_toml(
        dict(g, CIRCUITPY_WIFI_SSID="office-wifi")).encode(),
    "password_in_a_table": lambda g: ('[wifi]\nCIRCUITPY_WIFI_SSID = "office"\n'
                                      'CIRCUITPY_WIFI_PASSWORD = "%s"\n'
                                      % g[PASSWORD]).encode(),
    "foreign_ssid_literal": lambda g: b"CIRCUITPY_WIFI_SSID = 'office'\n",
}


@pytest.mark.parametrize("secrets_name", [fs.GOOD_SECRETS], indirect=True)
@pytest.mark.parametrize("bad", sorted(BAD))
def test_guard_stops_the_run(request, monkeypatch, secrets_name, generated_secrets,
                             good_secrets, bad):
    """secrets_path calls pytest.exit (returncode 3), not skip or fail, when a scenario's bytes
    could leak the farm password or point a board off the farm."""
    monkeypatch.setitem(generated_secrets, secrets_name, BAD[bad](good_secrets))
    with pytest.raises(pytest.exit.Exception, match="prod guard: scenario good") as exc:
        request.getfixturevalue("secrets_path")
    assert exc.value.returncode == 3
    assert DUMMY_PASSWORD not in str(exc.value)  # never print what it holds


@pytest.mark.parametrize("secrets_name", [fs.GOOD_SECRETS], indirect=True)
def test_guard_stops_on_an_aio_environment(request, monkeypatch, secrets_name):
    monkeypatch.setenv("ADAFRUIT_AIO_KEY", "x")
    with pytest.raises(pytest.exit.Exception, match="ADAFRUIT_AIO_KEY"):
        request.getfixturevalue("secrets_path")


def _session(cfg, dry_run=False, worker=False):
    config = types.SimpleNamespace(cpr_cfg=cfg, getoption=lambda name: dry_run)
    if worker:
        config.workerinput = {}
    return types.SimpleNamespace(config=config)


@pytest.mark.parametrize("env,trips", [
    ({"CPR_WIFI_SSID": "foreverrun"}, False),
    ({"CPR_WIFI_SSID": "office"}, True),
    ({"CPR_WIFI_SSID": "foreverrun", "IO_KEY": "x"}, True),
    ({"CPR_WIFI_SSID": "foreverrun", "SOMETHING": "aio_" + "b" * 28}, True),
])
def test_session_start_guard(monkeypatch, env, trips):
    """pytest_sessionstart stops the run before any board is opened; xdist workers and
    --cpr-dry-run leave it to the controller."""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = {"network": {"allowed_ssids": ["foreverrun"], "ssid_env": "CPR_WIFI_SSID"},
           "host": {"ip": BROKER}}
    if trips:
        with pytest.raises(pytest.exit.Exception, match="prod guard") as exc:
            fs.pytest_sessionstart(_session(cfg))
        assert exc.value.returncode == 3
    else:
        fs.pytest_sessionstart(_session(cfg))
    fs.pytest_sessionstart(_session(cfg, dry_run=True))
    fs.pytest_sessionstart(_session(cfg, worker=True))


def test_session_start_guard_refuses_a_public_host(monkeypatch):
    monkeypatch.delenv("CPR_WIFI_SSID", raising=False)
    cfg = {"network": {"allowed_ssids": ["foreverrun"]}, "host": {"ip": "8.8.8.8"}}
    with pytest.raises(pytest.exit.Exception, match="not a private address"):
        fs.pytest_sessionstart(_session(cfg))
