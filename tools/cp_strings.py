#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Build data/cp-strings.json: every translation of the supervisor and traceback lines the
harness waits on, from circuitpython's locale/*.po at one or more git refs.

A board can run a translated build (the farm's Feather ESP32-C6 runs ja, the ESP32-C5 ru,
2026-10-20), and then "soft reboot" arrives as "ソフトリブート". lib/cpstrings.py maps the
translations back to English, so the tests keep one set of strings.

Translations come and go between commits (ja "soft reboot" is filled at 3607c32e85 and empty
at 7dc7502aed), so pass the refs of the builds on the farm plus main; the table is their union.

    tools/cp_strings.py --repo ~/circuitpython 3607c32e85 be8692376f origin/main
"""
import argparse
import json
import os
import re
import subprocess
import sys

# msgids as in locale/circuitpython.pot (main.c, py/obj.c, supervisor/shared/safe_mode.c).
LITERAL = [
    "soft reboot\n",
    "\nCode done running.\n",
    "\nCode stopped by auto-reload. Reloading soon.\n",
    " output:\n",
    "Auto-reload is off.\n",
    "Auto-reload is on. Simply save files over USB to run them or enter REPL to disable.\n",
    "Press any key to enter the REPL. Use CTRL-D to reload.\n",
    "Traceback (most recent call last):\n",
    "Running in safe mode! Not running saved code.\n",
    "\nYou are in safe mode because:\n",
    "You pressed the BOOT button at start up",
    "You pressed the BOOT button at start up.",
    "CircuitPython core code crashed hard. Whoops!\n",
    "Hard fault: memory access or instruction error.",
    "\nPress reset to exit safe mode.\n",
]
# printf-style msgids: %q is a qstr (a file name or <module>), %d a number.
FORMAT = [
    '  File "%q", line %d',
    ", in %q\n",
]
# How the FORMAT lines print in English, to check no translation matches English output.
ENGLISH_SAMPLE = 'Traceback (most recent call last):\n  File "code.py", line 42, in <module>\n'
QSTR_RE = r'(<\w+>|[A-Za-z_][\w./-]*)'


def _unquote(s):
    return json.loads('"' + s + '"') if s else ""


def parse_po(text):
    """{msgid: msgstr} for one .po file; multi-line strings joined, fuzzy entries kept out."""
    out, msgid, msgstr, cur, fuzzy = {}, None, None, None, False

    def flush():
        if msgid and msgstr and not fuzzy:
            out[msgid] = msgstr

    for line in text.splitlines():
        if line.startswith("#,") and "fuzzy" in line:
            fuzzy = True
        elif line.startswith("msgid "):
            flush()
            msgid, msgstr, cur = _unquote(line[6:].strip()[1:-1]), None, "id"
        elif line.startswith("msgstr "):
            msgstr, cur = _unquote(line[7:].strip()[1:-1]), "str"
        elif line.startswith('"') and cur:
            piece = _unquote(line.strip()[1:-1])
            if cur == "id":
                msgid += piece
            else:
                msgstr += piece
        elif not line.strip():
            flush()
            msgid, msgstr, cur, fuzzy = None, None, None, False
    flush()
    return out


def _format_regex(fmt):
    """A translated printf format as a regex with one group per %q / %d, in order."""
    parts, order = [], []
    for tok in re.split(r"(%[qd])", fmt.strip("\n")):
        if tok == "%q":
            parts.append(QSTR_RE)
            order.append("q")
        elif tok == "%d":
            parts.append(r"(\d+)")
            order.append("d")
        else:
            parts.append(re.escape(tok))
    return "".join(parts), order


def build(po_texts):
    literal, fmt = {}, {}
    for text in po_texts:
        po = parse_po(text)
        for msgid in LITERAL:
            tr = po.get(msgid, "").strip()
            if tr and tr != msgid.strip():
                literal.setdefault(tr, msgid.strip())
        for msgid in FORMAT:
            tr = po.get(msgid, "")
            if not tr or tr == msgid:
                continue
            regex, order = _format_regex(tr)
            want, want_order = _format_regex(msgid)
            if sorted(order) != sorted(want_order) or not regex.strip("\\ "):
                continue
            # English with \1, \2 ... in the English order of the placeholders.
            eng, groups = "", {k: [n + 1 for n, o in enumerate(order) if o == k] for k in "qd"}
            for tok in re.split(r"(%[qd])", msgid.strip("\n")):
                if tok in ("%q", "%d"):
                    eng += "\\%d" % groups[tok[1]].pop(0)
                else:
                    eng += tok
            fmt.setdefault(regex, eng)
    # A translation that matches English output would rewrite it (one renders ", in %q" so
    # that "in <module>" matches): keep only those that leave every English line as it is.
    english = "\n".join(m.strip() for m in LITERAL) + "\n" + ENGLISH_SAMPLE
    literal = {t: e for t, e in literal.items() if t not in english}
    fmt = {r: e for r, e in fmt.items() if not re.search(r, english)}
    # Longest first, so a translation that contains another is replaced whole.
    return {"literal": sorted(([t, e] for t, e in literal.items()), key=lambda x: -len(x[0])),
            "format": sorted(([r, e] for r, e in fmt.items()), key=lambda x: -len(x[0]))}


def po_files_at(repo, ref):
    names = subprocess.run(["git", "-C", repo, "ls-tree", "--name-only", ref, "locale/"],
                           capture_output=True, text=True, check=True).stdout.split()
    for name in names:
        if name.endswith(".po"):
            yield subprocess.run(["git", "-C", repo, "show", "%s:%s" % (ref, name)],
                                 capture_output=True, text=True, check=True).stdout


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=os.path.expanduser("~/circuitpython"))
    ap.add_argument("-o", "--out", default=os.path.join(os.path.dirname(__file__), "..", "data",
                                                         "cp-strings.json"))
    ap.add_argument("refs", nargs="+")
    a = ap.parse_args(argv)
    texts = [t for ref in a.refs for t in po_files_at(a.repo, ref)]
    table = build(texts)
    table["refs"] = a.refs
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(table, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print("%d literal, %d format translations from %d .po files -> %s"
          % (len(table["literal"]), len(table["format"]), len(texts), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
