# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""English for translated CircuitPython builds.

A board on a translated build prints "ソフトリブート" for "soft reboot", and the tests wait for
the English. to_english() replaces every known translation of the supervisor and traceback
lines (data/cp-strings.json, built by tools/cp_strings.py from locale/*.po) with the English,
so the serial readers can hand the tests one language. Text in English is left as it is.
"""
import json
import os
import re

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "cp-strings.json")
_table = None


def _load():
    global _table  # pylint: disable=global-statement
    if _table is None:
        try:
            with open(_PATH, encoding="utf-8") as f:
                t = json.load(f)
        except (OSError, ValueError):
            t = {}
        _table = ([(a, b) for a, b in t.get("literal", [])],
                  [(re.compile(r), e) for r, e in t.get("format", [])])
    return _table


def normalize(text):
    """to_english(), and "\r\r\n" line ends folded to "\r\n": the ESP32-S31 Korvo-1 build
    (11.0.0-alpha.1-140) ends every console line with an extra CR ("42\r\r\n")."""
    text = to_english(text)
    if isinstance(text, (bytes, bytearray)):
        return bytes(text).replace(b"\r\r\n", b"\r\n")
    return text.replace("\r\r\n", "\r\n")


def to_english(text):
    """text (str or bytes) with known translations replaced by the English; same type back."""
    if isinstance(text, (bytes, bytearray)):
        s = bytes(text).decode("utf-8", errors="surrogateescape")
        return to_english(s).encode("utf-8", errors="surrogateescape")
    literal, fmt = _load()
    for tr, en in literal:
        if tr in text:
            text = text.replace(tr, en)
    for rx, en in fmt:
        text = rx.sub(en, text)
    return text
