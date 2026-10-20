# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Settings.toml parsing via os.getenv: types, escapes, unicode, defaults, malformed resilience.

pytest version of tests/settings_toml.py. The board's settings.toml is replaced with
TEST_SETTINGS (restored by md5 afterwards), the board soft-reboots to load it, and
DEVICE_CHECK runs once per board; each check it reports is one test here, in the same order.
Targets:
  #11118 - unicode in settings.toml values
  #7677 - escaped characters in settings.toml
"""
import pytest

from fixtures.device import DeviceResults

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.targets(11118, 7677),
    pytest.mark.destructive,
]

# settings.toml under test. On CircuitPython 11 main, os.getenv() returns every value as a
# str, and supervisor.get_setting() returns str / int / bool / float (raising ValueError on
# a bad value). Expectations follow the toml.io spec: lowercase inf/nan only, a bare sign is
# not a number, leading-zero ints are invalid.
TEST_SETTINGS = """plain_string = "hello"
escaped_string = "line1\\nline2"
unicode_escape = "emoji\\u2764"
unicode_direct = "caf\u00e9"
test_int = 42
negative_int = -17
hex_int = 0x1F
flag_true = true
ratio = 1.5
big_exp = 2e3
pos_inf = +inf
not_a_number = nan
bad_case_inf = Inf
bare_plus = +
lead_zero = 017
# a comment line
bad_value = this_is_not_toml
"""

DEVICE_CHECK = r'''
import os


def _cpr_main():
    try:
        os.getenv("plain_string")
    except NotImplementedError:
        skip("getenv", "settings.toml reading not in this build")
        return
    # os.getenv: strings (on 11.x even for ints)
    v = os.getenv("plain_string")
    check("getenv_plain", v == "hello", repr(v))
    v = os.getenv("escaped_string")
    check("getenv_escape_newline", v == "line1\nline2", repr(v))
    v = os.getenv("unicode_escape")
    check("getenv_unicode_escape", v == "emoji\u2764", repr(v))
    v = os.getenv("unicode_direct")
    check("getenv_unicode_direct", v == "caf\u00e9", repr(v))
    v = os.getenv("test_int")
    check("getenv_int_value", v in ("42", 42), repr(v))
    check("getenv_missing_none", os.getenv("no_such_key") is None, "")
    check("getenv_default", os.getenv("no_such_key", "dflt") == "dflt", "")
    check("getenv_after_bad_value", os.getenv("plain_string") == "hello", "bad lines must not break lookups")

    try:
        import supervisor
        gs = supervisor.get_setting
    except (ImportError, AttributeError):
        gs = None
    if gs is None:
        skip("get_setting", "supervisor.get_setting not in this build (added for 11.x)")
    else:
        def expect(name, key, want):
            try:
                v = gs(key)
                check(name, v == want and type(v) == type(want), "%r %s" % (v, type(v).__name__))
            except Exception as e:
                check(name, False, "%s: %s" % (type(e).__name__, e))

        def expect_error(name, key):
            try:
                v = gs(key)
                check(name, False, "accepted %r" % (v,))
            except ValueError:
                check(name, True, "")
            except Exception as e:
                check(name, False, "%s: %s" % (type(e).__name__, e))

        expect("gs_str", "plain_string", "hello")
        expect("gs_int", "test_int", 42)
        expect("gs_negative_int", "negative_int", -17)
        expect("gs_hex_int", "hex_int", 31)
        expect("gs_bool", "flag_true", True)
        expect("gs_float", "ratio", 1.5)
        expect("gs_float_exp", "big_exp", 2000.0)
        try:
            v = gs("pos_inf")
            check("gs_inf", v == float("inf"), repr(v))
            v = gs("not_a_number")
            check("gs_nan", v != v, repr(v))
        except Exception as e:
            check("gs_inf", False, "%s: %s" % (type(e).__name__, e))
        expect_error("gs_rejects_Inf", "bad_case_inf")
        expect_error("gs_rejects_bare_plus", "bare_plus")
        expect_error("gs_rejects_bad_value", "bad_value")
        check("gs_missing_default", gs("no_such_key", 7) == 7, "")
        try:
            v = gs("lead_zero")
            skip("spec_leading_zero", "accepted %r; toml.io says leading zeros are invalid" % (v,))
        except ValueError:
            check("spec_leading_zero", True, "rejected as the spec requires")


_cpr_main()
'''

# Checks DEVICE_CHECK reports after the os.getenv gate (skip "getenv" if the build lacks it).
GETENV_CHECKS = [
    "getenv_plain",
    "getenv_escape_newline",
    "getenv_unicode_escape",
    "getenv_unicode_direct",
    "getenv_int_value",
    "getenv_missing_none",
    "getenv_default",
    "getenv_after_bad_value",
]

# Checks DEVICE_CHECK reports after the supervisor.get_setting gate (skip "get_setting").
GS_CHECKS = [
    "gs_str",
    "gs_int",
    "gs_negative_int",
    "gs_hex_int",
    "gs_bool",
    "gs_float",
    "gs_float_exp",
    "gs_inf",
    "gs_nan",
    "gs_rejects_Inf",
    "gs_rejects_bare_plus",
    "gs_rejects_bad_value",
    "gs_missing_default",
    "spec_leading_zero",
]


def _why(e):
    return "%s: %s" % (type(e).__name__, e)


@pytest.fixture(scope="module")
def run_record(board):
    """Write TEST_SETTINGS, soft-reboot, run DEVICE_CHECK, restore, soft-reboot again.

    Returns what it saw. Exceptions are kept in the record so one failing step fails one test.
    """
    rec = {"present": board.present(), "written": None, "results": None, "error": None,
           "backup_error": None, "restore_error": None, "restored": None}
    entered = False
    try:
        # Use preserved() context to write and restore settings.toml
        with board.preserved(["settings.toml"]):
            entered = True
            try:
                board.write_file("settings.toml", TEST_SETTINGS)
                rec["written"] = True
                # Soft reboot to load new settings, then run the device checks
                with board.repl() as repl:
                    repl.soft_reboot()
                    repl.prompt()
                    try:
                        res, out, err = repl.run_device(DEVICE_CHECK, timeout=30)
                        rec["results"] = DeviceResults(board, res, out, err)
                    except Exception as e:  # pylint: disable=broad-except
                        rec["results"] = DeviceResults(board, [], "", "", crashed=_why(e))
            except Exception as e:  # pylint: disable=broad-except
                rec["error"] = _why(e)
    except Exception as e:  # pylint: disable=broad-except
        # backing up (before the block) or restoring (md5 check, after it) settings.toml failed
        rec["restore_error" if entered else "backup_error"] = _why(e)
    finally:
        # After preserved() exits, original settings.toml is restored.
        # Soft reboot again to return to normal state.
        try:
            with board.repl() as repl:
                repl.soft_reboot()
                repl.prompt()
            rec["restored"] = True
        except Exception as e:  # pylint: disable=broad-except
            rec["restored"] = _why(e)
    return rec


@pytest.fixture(scope="module")
def results(run_record):
    """DEVICE_CHECK's DeviceResults; skips as not reached if setup failed before it ran."""
    if run_record["results"] is None:
        pytest.skip("not reached: %s" % (run_record["error"] or run_record["backup_error"]
                                         or "device code did not run"))
    return run_record["results"]


def _gated(results, gate, name):
    """expect(name), but skip when the device skipped the gate check that guards it."""
    rs = results.checks.get(gate)
    if rs and all(r.get("ok") is None for r in rs) and name not in results.checks:
        pytest.skip(rs[0].get("detail") or "%s skipped on the device" % gate)
    results.expect(name)


def test_board_present(board, run_record):
    """The board is on the bus at its hub port."""
    assert run_record["present"], board.usb


def test_settings_written(run_record):
    """TEST_SETTINGS was written to CIRCUITPY/settings.toml."""
    assert run_record["written"], (run_record["error"] or run_record["backup_error"]
                                   or "settings.toml not written")


def _gate(results, gate, first):
    """A gate check: the device only reports it as a skip. Pass when the code got past it."""
    if gate in results.checks:
        results.expect(gate)
    elif first not in results.checks:
        results.expect(first)  # never got there: skips as not reached, or fails if silent


def test_getenv(results):
    """os.getenv works in this build (the device skips everything below if not)."""
    _gate(results, "getenv", GETENV_CHECKS[0])


@pytest.mark.parametrize("name", GETENV_CHECKS)
def test_getenv_check(results, name):
    """One os.getenv expectation against TEST_SETTINGS."""
    _gated(results, "getenv", name)


def test_get_setting(results):
    """supervisor.get_setting exists (11.x); the device skips the gs_* checks if not."""
    if "getenv" in results.checks and "get_setting" not in results.checks:
        _gated(results, "getenv", "get_setting")
    _gate(results, "get_setting", GS_CHECKS[0])


@pytest.mark.parametrize("name", GS_CHECKS)
def test_get_setting_check(results, name):
    """One supervisor.get_setting expectation (types, toml.io spec rejections)."""
    if "getenv" in results.checks and name not in results.checks:
        _gated(results, "getenv", name)
    if name == "gs_nan" and name not in results.checks:
        # the original reports only gs_inf when gs("pos_inf") raises
        rs = results.checks.get("gs_inf")
        if rs and rs[-1].get("ok") is False:
            pytest.skip("not reached: gs_inf failed first (%s)" % rs[-1].get("detail", ""))
    _gated(results, "get_setting", name)


def test_device_exception(run_record):
    """DEVICE_CHECK ran to the end without raising, and the board kept answering."""
    res = run_record["results"]
    if res is None:
        # settings.toml was written but the soft reboot / REPL before the run failed
        assert not (run_record["written"] and run_record["error"]), run_record["error"]
        pytest.skip("not reached: %s" % (run_record["error"] or run_record["backup_error"]
                                         or "device code did not run"))
    assert not res.crashed, "device run failed: " + res.crashed[-300:]
    assert not res.err, res.err[-300:]


def test_restored_and_rebooted(run_record):
    """The original settings.toml is back (md5 checked) and the board soft-rebooted onto it."""
    assert run_record["restore_error"] is None, run_record["restore_error"]
    assert run_record["restored"] is True, run_record["restored"]
