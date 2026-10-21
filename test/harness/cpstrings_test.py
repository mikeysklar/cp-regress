# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""cpstrings.to_english on what translated builds printed on bravo, 2026-10-20: the Feather
ESP32-C6 runs a ja build (11.0.0-alpha.1-44-g3607c32e85), the ESP32-C5 a ru one."""
import pytest

import cpstrings
from tools import cp_strings

pytestmark = pytest.mark.smoke

JA = ("トレースバック（最新の呼び出しが末尾）:\r\n"
      '  ファイル "code.py", 行 92<module> 内の\r\n'
      "KeyboardInterrupt: \r\n\r\nコード実行完了\r\n"
      "REPLに入るため、エンターキーを押す。リーロードするため、Ctl-Dを入力する。\r\n"
      "ソフトリブート\r\n")


def test_ja_board_output_reads_as_english():
    en = cpstrings.to_english(JA)
    assert "Traceback (most recent call last):" in en
    assert '  File "code.py", line 92, in <module>' in en
    assert "Code done running." in en
    assert "soft reboot" in en


def test_bytes_in_bytes_out():
    assert cpstrings.to_english(JA.encode()).startswith(b"Traceback (most recent call last):")
    assert cpstrings.to_english(bytearray(b"\x04OK\xff")) == b"\x04OK\xff"  # not utf-8: kept


def test_ru_code_done():
    assert cpstrings.to_english("Программа закончила выполнение.") == "Code done running."


def test_english_is_untouched():
    s = 'Traceback (most recent call last):\r\n  File "code.py", line 3, in <module>\r\nsoft reboot'
    assert cpstrings.to_english(s) == s


def test_parse_po_joins_lines_and_skips_fuzzy_and_empty():
    po = cp_strings.parse_po('''msgid ""
msgstr "header"

msgid ""
"\\n"
"Code done running.\\n"
msgstr ""
"\\n"
"Fertig.\\n"

#, fuzzy
msgid "soft reboot\\n"
msgstr "weich\\n"

msgid "Auto-reload is off.\\n"
msgstr ""
''')
    assert po == {"\nCode done running.\n": "\nFertig.\n"}


def test_build_keeps_the_placeholder_order():
    po = ('msgid "  File \\"%q\\", line %d"\nmsgstr "  %d行目, \\"%q\\""\n\n'
          'msgid ", in %q\\n"\nmsgstr " (%q)\\n"\n')
    table = cp_strings.build([po])
    import re  # pylint: disable=import-outside-toplevel
    text = '  5行目, "code.py" (<module>)'
    for rx, en in table["format"]:
        text = re.sub(rx, en, text)
    assert text == '  File "code.py", line 5, in <module>'


def test_normalize_folds_the_s31_extra_cr():
    assert cpstrings.normalize(b"print(7)\r\n7\r\r\n>>> ") == b"print(7)\r\n7\r\n>>> "
    assert cpstrings.normalize("ソフトリブート\r\r\n") == "soft reboot\r\n"
