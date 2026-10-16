# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Secrets fixtures, after Brent's WipperSnapper harness: base_secrets and secrets, plus his
named scenarios (secrets_name, secrets_path, generated_secrets, ...) for settings.toml.

base_secrets (session) is the default settings.toml for the farm, built from the environment
(.env). The prod guard runs at session start: the run stops before any board is touched when the WiFi
network is not one farm.toml allows, when anything that looks like an Adafruit IO credential is
in the environment, or when the broker is not on a private address.

secrets (module) is base_secrets plus the test module's own SECRETS = {...} override, the same
shape as his per-module override. flashed_fs writes it to the board as settings.toml.

Named scenarios (after Brent's hil/firmware/conftest.py:252-366 @ cf64e03). He keeps one
<scenario>.secrets.json per scenario in a secrets dir and builds a few in generated_secrets.
Here the dir is data/settings-scenarios (--secrets-dir, else $CPR_SECRETS_DIR): templates named
<scenario>.settings.toml with @@SSID@@-style placeholders, and scenarios.toml, which says for
every scenario what TOML and CircuitPython should do with it. A test picks one the way his do:

    @pytest.mark.parametrize("secrets_name", ["default"], indirect=True)

secrets_path renders it into tmp_path (never into the repo) after scenario_guard has passed it:
the farm WiFi password only ever sits next to an allowed SSID, any other SSID is a placeholder
or starts with cpr-fake-, and broker hosts are private addresses.

Never print what these return.
"""
import copy
import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib  # type: ignore

# CircuitPython settings.toml key <- farm.toml [network] key naming the environment variable
FROM_ENV = {
    "CIRCUITPY_WIFI_SSID": "ssid_env",
    "CIRCUITPY_WIFI_PASSWORD": "password_env",
}
WEB_API_PASSWORD = "cpr-test"  # what web_workflow_test.py already uses
SSID = "CIRCUITPY_WIFI_SSID"
PASSWORD = "CIRCUITPY_WIFI_PASSWORD"

AIO_NAME = re.compile(r"(^|_)(AIO|ADAFRUIT_IO|IO_KEY|IO_USERNAME)(_|$)", re.I)
AIO_KEY = re.compile(r"^aio_[A-Za-z0-9]{28}$")

# Brent's names and his scenario layout (conftest.py:63-70 @ cf64e03)
HERE = Path(__file__).resolve().parents[2]
DEFAULT_SECRETS_DIR = HERE / "data" / "settings-scenarios"
SECRETS_DIR_ENV = "CPR_SECRETS_DIR"
SECRETS_SUFFIX = ".settings.toml"
MANIFEST = "scenarios.toml"
GOOD_SECRETS = "good"
DEFAULT_SECRETS = "default"
NO_SECRETS_FILE = "no_secrets_file"  # CIRCUITPY has no settings.toml at all
NO_FS_IMAGE = "no_fs_image"          # his erased fs partition; not ported, see scenarios.toml

# SSIDs a scenario may carry besides the allowed ones. None of them is a real network, and
# scenario_guard refuses them next to the farm password.
FAKE_SSID_PREFIX = "cpr-fake-"
PLACEHOLDER_SSID = "YOUR_WIFI_SSID_HERE"  # his placeholders, conftest.py:292-293 @ cf64e03
PLACEHOLDER_PASSWORD = "YOUR_WIFI_PASS_HERE"
WRONG_PASSWORD = "cpr-wrong-password"
PLACEHOLDER = re.compile(rb"@@[A-Z_]+@@")


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


def _values(cfg, broker_host, broker_port):
    net = cfg.get("network", {})
    values = {}
    for key, env_key in FROM_ENV.items():
        env_name = net.get(env_key)
        if env_name and os.environ.get(env_name):
            values[key] = os.environ[env_name]
    values["CIRCUITPY_WEB_API_PASSWORD"] = WEB_API_PASSWORD
    values["CPR_BROKER_HOST"] = broker_host
    values["CPR_BROKER_PORT"] = broker_port
    return values


def pytest_addoption(parser):
    """--secrets-dir, his option name (conftest.py:84 @ cf64e03)."""
    parser.getgroup("cp-regress").addoption(
        "--secrets-dir", default="",
        help="settings.toml scenario dir (default: $%s, else %s)"
             % (SECRETS_DIR_ENV, DEFAULT_SECRETS_DIR.relative_to(HERE)))


def pytest_sessionstart(session):
    """The prod guard runs here, before collection and before any board is opened, on the
    controller (or a run without xdist). base_secrets checks again for its own values."""
    config = session.config
    if hasattr(config, "workerinput") or config.getoption("cpr_dry_run"):
        return
    import farmlib  # pylint: disable=import-outside-toplevel
    cfg = config.cpr_cfg
    host = cfg.get("host", {}).get("ip") or farmlib.host_ip()
    values = _values(cfg, host, cfg.get("broker", {}).get("mqtt_port", 1884))
    why = prod_guard(cfg, os.environ, values, host)
    if why:
        pytest.exit("prod guard: " + "; ".join(why), returncode=3)


@pytest.fixture(scope="session")
def base_secrets(request, broker_address):
    """The farm's default settings.toml as a dict. Stops the run if the prod guard trips."""
    cfg = request.config.cpr_cfg
    values = _values(cfg, broker_address[0], broker_address[1])
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
    return '"%s"' % _escape(v)


def _escape(v):
    """The inside of a TOML basic string (toml.io v1.0.0 #string)."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def settings_toml(values):
    """Render a secrets dict as settings.toml text, keys sorted so the bytes are stable."""
    return "".join("%s = %s\n" % (k, toml_value(values[k])) for k in sorted(values))


# ---------------------------------------------------------------- named scenarios

@dataclass(frozen=True)
class BrokerCreds:
    """His BrokerCreds (conftest.py:238 @ cf64e03). ProtoMQ takes no login, so username and key
    stay empty: no Adafruit IO credential is ever put on a farm board."""
    host: str
    port: int
    username: str
    key: str


@dataclass(frozen=True)
class NetworkCreds:
    """His NetworkCreds (conftest.py:246 @ cf64e03)."""
    ssid: str
    password: str


class SecretsError(Exception):
    """A settings file that cannot reach a board: unreadable, not TOML, or a field missing or
    out of range. His SecretsError (hil_board_utils.py:97 @ cf64e03)."""


def require_string(obj, key, name=None, allow_empty=False):
    """Raise SecretsError unless obj[key] is a string. His require_string
    (hil_board_utils.py:272 @ cf64e03). name is how the message spells the key."""
    name = name or key
    if key not in obj:
        raise SecretsError(f"missing '{name}'")
    value = obj[key]
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        kind = "a string" if allow_empty else "a non-empty string"
        raise SecretsError(f"'{name}' must be {kind}")


def require_number(obj, key, lo, hi, integer=False):
    """Raise SecretsError unless obj[key] is a number (an integer when integer) in [lo, hi].
    His require_number (hil_board_utils.py:282 @ cf64e03). Booleans are refused."""
    if key not in obj:
        raise SecretsError(f"missing '{key}'")
    value = obj[key]
    types = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, types):
        raise SecretsError(f"'{key}' must be {'an integer' if integer else 'a number'}")
    if not lo <= value <= hi:
        raise SecretsError(f"'{key}' must be between {lo} and {hi}, got {value}")


def validate_settings(settings):
    """Check a settings.toml dict has what a farm board needs. His validate_secrets
    (hil_board_utils.py:293 @ cf64e03) checks secrets.json; ours checks settings.toml:

      CIRCUITPY_WIFI_SSID      non-empty string (required)
      CIRCUITPY_WIFI_PASSWORD  string, may be empty (required)
      CPR_BROKER_HOST          non-empty string (optional)
      CPR_BROKER_PORT          integer 1-65535 (optional)
    """
    if not isinstance(settings, dict):
        raise SecretsError("top level must be a table")
    require_string(settings, SSID)
    require_string(settings, PASSWORD, allow_empty=True)
    if "CPR_BROKER_HOST" in settings:
        require_string(settings, "CPR_BROKER_HOST")
    if "CPR_BROKER_PORT" in settings:
        require_number(settings, "CPR_BROKER_PORT", 1, 65535, integer=True)


def load_settings(path):
    """Read, parse and validate a settings.toml file and return the dict. Raises SecretsError
    naming the file for anything that would stop a board reading it. His load_secrets
    (hil_board_utils.py:328 @ cf64e03)."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise SecretsError(f"cannot read settings file: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise SecretsError(f"{path} is not valid TOML: {e}") from e
    try:
        validate_settings(data)
    except SecretsError as e:
        raise SecretsError(f"{path}: {e}") from e
    return data


def load_scenarios(directory):
    """{name: entry} from <directory>/scenarios.toml, with the optional fields filled in."""
    with open(Path(directory) / MANIFEST, "rb") as f:
        data = tomllib.load(f).get("scenario", {})
    for entry in data.values():
        for key in ("getenv_none", "getenv_present", "setting_error", "errors"):
            entry.setdefault(key, [])
        for key in ("getenv", "getenv_raises", "setting"):
            entry.setdefault(key, {})
        entry.setdefault("needs_wifi", False)
        entry.setdefault("needs_creds", False)
        entry.setdefault("connected", None)
    return data


def substitutions(good):
    """Placeholder -> bytes for templates, from the good (prod-guarded) secrets. SSID and
    password are left out when .env does not set them; scenarios using them then skip."""
    subs = {b"@@WEB_API_PASSWORD@@": _escape(WEB_API_PASSWORD).encode()}
    for name, key in (("SSID", SSID), ("PASSWORD", PASSWORD),
                      ("BROKER_HOST", "CPR_BROKER_HOST"), ("BROKER_PORT", "CPR_BROKER_PORT")):
        if good.get(key) not in (None, ""):
            subs[("@@%s@@" % name).encode()] = _escape(good[key]).encode()
    return subs


def fill(data, subs):
    """data (bytes) with its placeholders replaced. Raises KeyError naming one it cannot fill."""
    def one(m):
        return subs[m.group(0)]
    return PLACEHOLDER.sub(one, data)


def _with_wifi(secrets, **changes):
    """A copy of secrets with changes applied; None deletes a key. His _with_wifi
    (conftest.py:298 @ cf64e03) updates network_type_wifi; settings.toml is flat."""
    secrets = copy.deepcopy(secrets)
    for key, value in changes.items():
        if value is None:
            secrets.pop(key, None)
        else:
            secrets[key] = value
    return secrets


def _raw(values, *lines):
    """settings_toml(values) plus lines it cannot render (arrays, tables, bare words)."""
    return (settings_toml(values) + "".join(line + "\n" for line in lines)).encode()


def _fake_ssid(length):
    return (FAKE_SSID_PREFIX + "x" * length)[:length]


def build_generated(default, good):
    """{name: dict or bytes} for the scenarios built in code rather than read from the dir.
    A dict is rendered with settings_toml; bytes are written as they are. Scenarios that need
    the farm SSID and password are left out when .env does not set them.

    His generated_secrets (conftest.py:305-323 @ cf64e03) builds the structure cases from his
    placeholder secrets; the TOML ones below keep the farm's broker keys (base) so a test can
    see that the rest of the file still reads."""
    base = _with_wifi(good, CIRCUITPY_WIFI_SSID=None, CIRCUITPY_WIFI_PASSWORD=None)
    gen = {
        GOOD_SECRETS: dict(good),
        DEFAULT_SECRETS: dict(default),
        "empty": b"",
        "no_wifi_ssid": _with_wifi(good, CIRCUITPY_WIFI_SSID=None),
        "ssid_empty_array": _raw(base, "CIRCUITPY_WIFI_SSID = []"),
        "ssid_integer": _raw(base, "CIRCUITPY_WIFI_SSID = 12345"),
        "ssid_inline_table": _raw(base, 'CIRCUITPY_WIFI_SSID = { name = "cpr-fake-net" }'),
        "truncated_string": _raw(base) + b'CIRCUITPY_WIFI_SSID = "cpr-fake-trunc',
        "unknown_ssid": _with_wifi(base, CIRCUITPY_WIFI_SSID=FAKE_SSID_PREFIX + "no-such-network",
                                   CIRCUITPY_WIFI_PASSWORD=WRONG_PASSWORD),
        "ssid_32": _with_wifi(base, CIRCUITPY_WIFI_SSID=_fake_ssid(32)),
        "ssid_33": _with_wifi(base, CIRCUITPY_WIFI_SSID=_fake_ssid(33)),
        "invalid_utf8": _raw(base) + b'CPR_BAD_UTF8 = "\xff\xfe"\n',
    }
    if good.get(SSID) and good.get(PASSWORD):
        no_pw = _with_wifi(good, CIRCUITPY_WIFI_PASSWORD=None)
        gen.update({
            "no_wifi_password": no_pw,
            "wrong_password": _with_wifi(good, CIRCUITPY_WIFI_PASSWORD=WRONG_PASSWORD),
            "password_63": _with_wifi(good, CIRCUITPY_WIFI_PASSWORD=WRONG_PASSWORD.ljust(63, "x")),
            "password_64": _with_wifi(good, CIRCUITPY_WIFI_PASSWORD=WRONG_PASSWORD.ljust(64, "x")),
            "password_integer": _raw(no_pw, "CIRCUITPY_WIFI_PASSWORD = 12345678"),
            "ssid_empty_string": _with_wifi(good, CIRCUITPY_WIFI_SSID=""),
            "crlf": settings_toml(good).replace("\n", "\r\n").encode(),
            # SSID on the first line, so the BOM sits in front of its key
            "bom": (b"\xef\xbb\xbf" + ("%s = %s\n" % (SSID, toml_value(good[SSID]))).encode()
                    + settings_toml(_with_wifi(good, CIRCUITPY_WIFI_SSID=None)).encode()),
        })
    return gen


def render(value):
    """A generated_secrets entry as the bytes that go on the board."""
    return settings_toml(value).encode() if isinstance(value, dict) else bytes(value)


_LINE = re.compile(r"""^\s*(?:"([^"]*)"|'([^']*)'|([A-Za-z0-9_.-]+))\s*=\s*(.*?)\s*$""")


def _string_value(text):
    """The string a TOML-ish value spells, or None when it is not a quoted string. Lenient:
    this only has to find every SSID or host a parser might hand to the board."""
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote):
            end = text.find(quote, len(quote))
            body = text[len(quote):] if end < 0 else text[len(quote):end]
            if quote.startswith('"'):
                body = body.replace('\\"', '"').replace("\\\\", "\\")
            return body
    return None


def scenario_guard(cfg, env, data, password):
    """Reasons scenario bytes `data` must not reach a board, empty when they may. Pure.

    prod_guard's rules, applied to every line of the file instead of base_secrets: the farm
    WiFi password (`password`) only next to an allowed SSID, every other SSID a placeholder,
    empty, or cpr-fake-*, broker hosts private, and nothing that looks like Adafruit IO."""
    allowed = cfg.get("network", {}).get("allowed_ssids", [])
    why = prod_guard(cfg, env, {}, None)
    text = data.decode("utf-8", "replace").lstrip("﻿")
    if re.search(r"adafruit\.io|io\.adafruit\.com", text, re.I):
        why.append("names an Adafruit IO host")
    has_password = bool(password) and password.encode() in data
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        key = next(g for g in m.groups()[:3] if g is not None)
        value = _string_value(m.group(4))
        if AIO_NAME.search(key):
            why.append("has key %s; Adafruit IO credentials never go to farm boards" % key)
        if value is not None and AIO_KEY.match(value):
            why.append("%s holds an Adafruit IO key" % key)
        if "SSID" in key.upper() and value:
            if value in allowed:
                continue
            if has_password:
                why.append("pairs the farm WiFi password with an SSID not in allowed_ssids")
            elif not (value.startswith(FAKE_SSID_PREFIX) or value == PLACEHOLDER_SSID):
                why.append("%s is not allowed, a placeholder, or %s*" % (key, FAKE_SSID_PREFIX))
        if "BROKER_HOST" in key.upper() and value is not None:
            why += prod_guard({}, {}, {}, value or "''")
    return sorted(set(why))


@pytest.fixture(scope="session")
def secrets_dir(pytestconfig):
    """His secrets_dir (conftest.py:252 @ cf64e03): --secrets-dir, else $CPR_SECRETS_DIR,
    else data/settings-scenarios."""
    directory = (pytestconfig.getoption("secrets_dir")
                 or os.environ.get(SECRETS_DIR_ENV)
                 or DEFAULT_SECRETS_DIR)
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        pytest.fail(f"secrets dir {directory} not found: pass --secrets-dir or set "
                    f"${SECRETS_DIR_ENV}", pytrace=False)
    return directory


@pytest.fixture(scope="session")
def secrets_file(secrets_dir):
    """secrets_file(name) -> the path of <secrets_dir>/<name>.settings.toml (a template)."""
    def find(name):
        path = secrets_dir / f"{name}{SECRETS_SUFFIX}"
        if not path.is_file():
            pytest.fail(f"secrets scenario '{name}' not found: {path}", pytrace=False)
        return path
    return find


@pytest.fixture(scope="session")
def secrets_scenarios(secrets_dir):
    """{name: entry} from scenarios.toml: what TOML and CircuitPython should do with each."""
    try:
        return load_scenarios(secrets_dir)
    except (OSError, tomllib.TOMLDecodeError) as e:
        pytest.fail(f"cannot read {secrets_dir / MANIFEST}: {e}", pytrace=False)


@pytest.fixture(scope="session")
def good_secrets(base_secrets):
    """The known-good settings: base_secrets, already through the prod guard. His good_secrets
    (conftest.py:275 @ cf64e03) reads good.secrets.json; ours comes from .env."""
    return base_secrets


@pytest.fixture(scope="session")
def default_secrets():
    """His placeholder secrets (conftest.py:284 @ cf64e03) as settings.toml keys. CircuitPython
    has no placeholder check, so the board just fails to find the network. His io_username /
    io_key placeholders are left out: no Adafruit IO key name ever goes on a farm board."""
    settings = {SSID: PLACEHOLDER_SSID, PASSWORD: PLACEHOLDER_PASSWORD}
    validate_settings(settings)  # the placeholders must pass the same check as a real file
    return settings


@pytest.fixture(scope="session")
def generated_secrets(default_secrets, good_secrets):
    """Scenarios built here instead of read from the dir, by name (see build_generated)."""
    return build_generated(default_secrets, good_secrets)


@pytest.fixture(scope="session")
def broker_creds(good_secrets):
    """Where boards publish: the local ProtoMQ, never io.adafruit.com."""
    return BrokerCreds(host=good_secrets["CPR_BROKER_HOST"],
                       port=good_secrets["CPR_BROKER_PORT"], username="", key="")


@pytest.fixture(scope="session")
def network_creds(good_secrets):
    """The farm WiFi from .env. Skips when CPR_WIFI_SSID / CPR_WIFI_PASSWORD are not set."""
    if not good_secrets.get(SSID) or not good_secrets.get(PASSWORD):
        pytest.skip("CPR_WIFI_SSID and/or CPR_WIFI_PASSWORD not set")
    return NetworkCreds(ssid=good_secrets[SSID], password=good_secrets[PASSWORD])


@pytest.fixture
def secrets_name(request):
    """The settings scenario the test boots with: the test's indirect parameter, else the good
    secrets (conftest.py:345 @ cf64e03)."""
    return getattr(request, "param", GOOD_SECRETS)


@pytest.fixture
def secrets_path(request, secrets_name, secrets_file, tmp_path):
    """The rendered settings.toml for secrets_name in tmp_path, or None for no_secrets_file
    (his conftest.py:352 @ cf64e03). Templates from the dir have their placeholders filled from
    good_secrets; generated scenarios are rendered as they are. Either way scenario_guard runs
    first and stops the run, like the prod guard, if the file could leak the farm password or
    point a board at a public broker."""
    scenarios = request.getfixturevalue("secrets_scenarios")
    entry = scenarios.get(secrets_name)
    if entry is None:
        pytest.fail(f"secrets scenario '{secrets_name}' is not in {MANIFEST}", pytrace=False)
    if entry.get("source") == "not_ported":
        pytest.skip("not ported: " + entry.get("cp", ""))
    if secrets_name == NO_SECRETS_FILE:
        return None
    good = request.getfixturevalue("good_secrets")
    if entry["needs_creds"] and not (good.get(SSID) and good.get(PASSWORD)):
        pytest.skip("CPR_WIFI_SSID and/or CPR_WIFI_PASSWORD not set")
    generated = request.getfixturevalue("generated_secrets")
    if secrets_name in generated:
        data = render(generated[secrets_name])
    else:
        try:
            data = fill(secrets_file(secrets_name).read_bytes(), substitutions(good))
        except KeyError as e:
            pytest.skip("scenario needs %s, which .env does not set" % e.args[0].decode())
    why = scenario_guard(request.config.cpr_cfg, os.environ, data, good.get(PASSWORD))
    if why:
        pytest.exit("prod guard: scenario %s: %s" % (secrets_name, "; ".join(why)), returncode=3)
    path = tmp_path / f"{secrets_name}{SECRETS_SUFFIX}"
    path.write_bytes(data)
    return path
