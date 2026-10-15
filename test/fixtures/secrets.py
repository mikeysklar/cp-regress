# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Secrets fixtures, after Brent's WipperSnapper harness: base_secrets and secrets.

base_secrets (session) is the default settings.toml for the farm, built from the environment
(.env). It is also the prod guard: the run stops before any board is touched when the WiFi
network is not one farm.toml allows, when anything that looks like an Adafruit IO credential is
in the environment, or when the broker is not on a private address.

secrets (module) is base_secrets plus the test module's own SECRETS = {...} override, the same
shape as his per-module override. flashed_fs writes it to the board as settings.toml.

Never print what these return.
"""
import ipaddress
import os
import re

import pytest

# CircuitPython settings.toml key <- farm.toml [network] key naming the environment variable
FROM_ENV = {
    "CIRCUITPY_WIFI_SSID": "ssid_env",
    "CIRCUITPY_WIFI_PASSWORD": "password_env",
}
WEB_API_PASSWORD = "cpr-test"  # what web_workflow_test.py already uses

AIO_NAME = re.compile(r"(^|_)(AIO|ADAFRUIT_IO|IO_KEY|IO_USERNAME)(_|$)", re.I)
AIO_KEY = re.compile(r"^aio_[A-Za-z0-9]{28}$")


def prod_guard(cfg, env, values, broker_host):
    """Reasons this run must not start, empty when it may. Pure, so it is unit-testable."""
    why = []
    allowed = cfg.get("network", {}).get("allowed_ssids", [])
    ssid = values.get("CIRCUITPY_WIFI_SSID")
    if ssid and ssid not in allowed:
        why.append("WiFi network is not in farm.toml [network] allowed_ssids")
    for name, value in env.items():
        if AIO_NAME.search(name):
            why.append("environment has %s; Adafruit IO credentials never go to farm boards" % name)
        elif value and AIO_KEY.match(value):
            why.append("environment variable %s holds an Adafruit IO key" % name)
    try:
        if broker_host and not ipaddress.ip_address(broker_host).is_private:
            why.append("broker %s is not a private address" % broker_host)
    except ValueError:
        why.append("broker host %r is not an IP address" % broker_host)
    return why


@pytest.fixture(scope="session")
def base_secrets(request, broker_address):
    """The farm's default settings.toml as a dict. Stops the run if the prod guard trips."""
    cfg = request.config.cpr_cfg
    net = cfg.get("network", {})
    values = {}
    for key, env_key in FROM_ENV.items():
        env_name = net.get(env_key)
        if env_name and os.environ.get(env_name):
            values[key] = os.environ[env_name]
    values["CIRCUITPY_WEB_API_PASSWORD"] = WEB_API_PASSWORD
    values["CPR_BROKER_HOST"] = broker_address[0]
    values["CPR_BROKER_PORT"] = broker_address[1]
    why = prod_guard(cfg, os.environ, values, broker_address[0])
    if why:
        pytest.exit("prod guard: " + "; ".join(why), returncode=3)
    return values


@pytest.fixture(scope="module")
def secrets(request, base_secrets):
    """base_secrets with the test module's SECRETS override applied (None deletes a key)."""
    merged = dict(base_secrets)
    for key, value in getattr(request.module, "SECRETS", {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def toml_value(v):
    """A settings.toml value. CircuitPython only treats "..." as a string."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')


def settings_toml(values):
    """Render a secrets dict as settings.toml text, keys sorted so the bytes are stable."""
    return "".join("%s = %s\n" % (k, toml_value(values[k])) for k in sorted(values))
