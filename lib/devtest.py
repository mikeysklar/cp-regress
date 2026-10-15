# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
# cp-regress on-device result helper. The host prepends this to every device
# test body and parses lines that start with "@@R ". Keep it tiny and
# dependency-free: it must run on a SAMD21 with no json module.


def _cpr_enc(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    out = '"'
    for c in s:
        o = ord(c)
        if c == '"' or c == "\\":
            out += "\\" + c
        elif o < 32:
            out += "\\u%04x" % o
        else:
            out += c
    return out + '"'


def _cpr_emit(**kw):
    print("@@R {" + ", ".join(_cpr_enc(k) + ": " + _cpr_enc(v) for k, v in kw.items()) + "}")


def check(name, ok, detail="", value=None):
    _cpr_emit(name=name, ok=bool(ok), detail=detail, value=value)
    return ok


def skip(name, why=""):
    _cpr_emit(name=name, ok=None, detail=why)


def metric(name, value, unit="", better="lower"):
    # better: "lower" (times) or "higher" (rates, counts). The host compares
    # against the median of earlier runs on the same board.
    _cpr_emit(name=name, ok=True, kind="metric", value=value, unit=unit, better=better)


def golden(name, value):
    # The host stores the first value seen on this board and fails later runs that differ.
    _cpr_emit(name=name, ok=True, kind="golden", value=value)


def guard(name, fn, *args):
    """Run fn; a raised exception becomes a failed check, never a dead test."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - we want every failure as a result
        check(name, False, "%s: %s" % (type(e).__name__, e))
        return None
