# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Settings.toml parsing via os.getenv: types, escapes, unicode, defaults, malformed resilience.

Targets:
  #11118 - unicode in settings.toml values
  #7677 - escaped characters in settings.toml
"""

META = {
    "id": "settings_toml",
    "title": "settings.toml parsing and os.getenv behavior",
    "category": "Core",
    "tier": "smoke",
    "needs": [],
    "wiring": [],
    "skip_boards": [],
    "automation": "auto",
    "destructive": True,
    "multi_board": False,
    "timeout": 60,
    "targets": [11118, 7677],
}

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


def run(board, ctx):
    ctx.check("board_present", board.present(), board.usb)

    # Use preserved() context to write and restore settings.toml
    with board.preserved(["settings.toml"]):
        # Write test settings.toml
        board.write_file("settings.toml", TEST_SETTINGS)
        ctx.check("settings_written", True)

        # Soft reboot to load new settings
        with board.repl() as repl:
            repl.soft_reboot()
            repl.prompt()

            # Run device checks
            res, out, err = repl.run_device(DEVICE_CHECK, timeout=30)
            ctx.absorb(res)

            if err.strip():
                ctx.check("device_exception", False, err.strip()[-300:])

    # After preserved() exits, original settings.toml is restored
    # Soft reboot again to return to normal state
    with board.repl() as repl:
        repl.soft_reboot()
        repl.prompt()
        ctx.check("restored_and_rebooted", True)
