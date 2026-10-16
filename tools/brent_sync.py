#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Keep cp-regress aligned with Brent's WipperSnapper HIL harness while he keeps changing it.

Reads docs/BRENT-SYNC.md (last-synced shas, the mapping table, the not-ported list), asks
GitHub what changed on each tracked branch since then, and maps every changed file and every
changed function, class or constant to the cp-regress file that ports it:

  python3 tools/brent_sync.py                    # all three repos, via gh api
  python3 tools/brent_sync.py brent -v           # one repo, list every changed file
  python3 tools/brent_sync.py --via git          # cached partial clone instead of gh api
  python3 tools/brent_sync.py --repo-dir brent=~/src/brent-hil --since brent=85bac77
  python3 tools/brent_sync.py --symbols brent    # every tracked symbol at the head, mapped
  python3 tools/brent_sync.py --record           # after review: write new shas and lines

Tracked symbols: Python top-level functions, classes, methods and UPPER_CASE constants (by
AST, so comment and formatting changes do not count); ProtoMQ HTTP routes and JS exports;
.proto messages, enums and services; top-level keys of JSON files (hil-boards.json).

A changed symbol is UNMAPPED when no row in the mapping or not-ported tables matches it. A
new symbol that only matches a wildcard or file-level row is listed as "new" so someone
confirms the row still fits. Citations like "hil_board_utils.py:757 @ cf64e03" anywhere in
this repo are checked too: a cited symbol that changed or moved is reported with its new line.

Read-only against GitHub: gh api GETs, git clone/fetch. Nothing is pushed or commented. The
only file it writes is docs/BRENT-SYNC.md, and only with --record.

Exit status: 0 nothing new, 2 new upstream commits and everything is mapped, 3 something is
unmapped, 1 an error (network, bad table, unknown sha).
"""
import argparse
import ast
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(HERE, "docs", "BRENT-SYNC.md")
CACHE = os.path.expanduser("~/.cache/cp-regress/brent-sync")
SEC_SYNC, SEC_MAP, SEC_SKIP = "Last synced", "Mapping", "Deliberately not ported"
CITE = re.compile(r"([\w./-]+\.(?:py|js|json|proto)):(\d+)\s*@\s*([0-9a-f]{7,40})\b")
JS_ROUTE = re.compile(r"""\.(get|post|put|patch|delete)\(\s*['"`](/[^'"`]*)['"`]""")
JS_EXPORT = re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|const|let|class)"
                       r"\s+(\w+)", re.M)
JS_BLOCK = re.compile(r"/\*.*?\*/", re.S)
JS_LINE = re.compile(r"(^|\s)//.*$", re.M)
PROTO_DEF =re.compile(r"^\s*(?:message|enum|service)\s+(\w+)", re.M)
CONST = re.compile(r"^_?[A-Z][A-Z0-9_]*$")


class SyncError(Exception):
    pass


def run(cmd, cwd=None, ok=(0,)):
    """Run cmd, return stdout bytes. Raises SyncError with stderr when the exit is not in ok."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=600)
    except OSError as e:
        raise SyncError("cannot run %s: %s" % (cmd[0], e)) from e
    except subprocess.TimeoutExpired as e:
        raise SyncError("timed out: %s" % " ".join(cmd)) from e
    if p.returncode not in ok:
        raise SyncError("%s failed (exit %d): %s" % (" ".join(cmd[:4]), p.returncode,
                                                      p.stderr.decode(errors="replace").strip()))
    return p.stdout


# --- docs/BRENT-SYNC.md ------------------------------------------------------------------------


def _cell(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == "`":
        s = s[1:-1]
    return s


def read_tables(text):
    """{section heading: [row dict, ...]} for the first pipe table under each '## ' heading.
    Row dicts are keyed by the lower-cased header cells; each also carries '_line' (0-based)."""
    tables, section, header = {}, None, None
    for i, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            section, header = line[3:].strip(), None
            continue
        if section is None or not line.startswith("|"):
            if header is not None and section in tables:
                section = None  # first table only
            continue
        cells = [_cell(c) for c in line.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            tables[section] = []
        elif all(set(c) <= set("-: ") for c in cells):
            continue
        else:
            row = dict(zip(header, cells))
            row["_line"] = i
            tables[section].append(row)
    return tables


def load_doc(path=DOC):
    with open(path) as f:
        text = f.read()
    t = read_tables(text)
    for sec in (SEC_SYNC, SEC_MAP, SEC_SKIP):
        if sec not in t:
            raise SyncError("%s has no '## %s' table" % (path, sec))
    repos = {}
    for r in t[SEC_SYNC]:
        if not all(r.get(c) for c in ("key", "repo", "branch", "sha")):
            raise SyncError("%s line %d: '%s' rows need Key, Repo, Branch and SHA"
                            % (path, r["_line"] + 1, SEC_SYNC))
        watch = r.get("watch", "*").split()
        repos[r["key"]] = {"key": r["key"], "repo": r["repo"], "branch": r["branch"],
                           "sha": r["sha"], "watch": [w for w in watch if not w.startswith("!")],
                           "ignore": [w[1:] for w in watch if w.startswith("!")], "_row": r}
    rules = []
    for kind, sec in (("map", SEC_MAP), ("skip", SEC_SKIP)):
        for r in t[sec]:
            key = r.get("brent", "")
            if r.get("repo") not in repos:
                raise SyncError("%s line %d: repo %r is not in the '%s' table"
                                % (path, r["_line"] + 1, r.get("repo"), SEC_SYNC))
            if key in ("", "-"):
                continue  # a concept row with no single upstream symbol
            rules.append(dict(r, kind=kind, key=key))
    return text, repos, rules


def watched(repo, path):
    if any(fnmatch.fnmatchcase(path, g) for g in repo["ignore"]):
        return False
    return any(fnmatch.fnmatchcase(path, g) for g in repo["watch"])


def _specificity(pattern):
    sym = "::" in pattern
    wild = any(c in pattern for c in "*?[")
    return (sym, not wild, len(pattern))


def rule_matches(pattern, key):
    """pattern 'path' matches the file and all its symbols; 'path::Sym' matches Sym and, for a
    class, its methods Sym.x. '*' crosses '/' (fnmatch)."""
    path, _, sym = key.partition("::")
    ppath, has_sym, psym = pattern.partition("::")
    if not fnmatch.fnmatchcase(path, ppath):
        return False
    if not has_sym:
        return True
    if not sym:
        return False
    return fnmatch.fnmatchcase(sym, psym) or sym.startswith(psym + ".")


def lookup(rules, repo_key, key):
    """The most specific mapping or not-ported row for key, or None."""
    best = None
    for r in rules:
        if r["repo"] == repo_key and rule_matches(r["key"], key):
            rank = _specificity(r["key"]) + (r["kind"] == "map",)
            if best is None or rank > best[0]:
                best = (rank, r)
    return best and best[1]


def loose(rule, key):
    """True when key matched only through a wildcard or a file-level row."""
    return "::" in key and (not _specificity(rule["key"])[1] or "::" not in rule["key"])


def ours_paths(rule):
    """Repo-relative paths named in the 'ours' cell, for an existence check."""
    out = []
    for part in re.split(r"[,;]", rule.get("ours", "")):
        tok = part.strip().strip("`").split()
        if not tok or tok[0] in ("-", "—"):
            continue
        p = tok[0].split("::")[0].rstrip(":")
        if "/" in p or p.endswith((".py", ".toml", ".md")):
            out.append(p)
    return out


# --- symbols -----------------------------------------------------------------------------------


def _fp(text):
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def py_symbols(src):
    """{name: (line, end_line, fingerprint)} for top-level defs/classes/CONSTANTS and methods."""
    tree = ast.parse(src)
    out = {}

    def add(name, node, body=None):
        dump = ast.dump(node) if body is None else "".join(ast.dump(b) for b in body)
        out[name] = (node.lineno, getattr(node, "end_lineno", node.lineno), _fp(dump))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node.name, node)
        elif isinstance(node, ast.ClassDef):
            funcs = (ast.FunctionDef, ast.AsyncFunctionDef)
            meths = [b for b in node.body if isinstance(b, funcs)]
            rest = [b for b in node.body if b not in meths]
            add(node.name, node, rest + node.decorator_list + node.bases)
            for m in meths:
                add("%s.%s" % (node.name, m.name), m)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and CONST.match(t.id):
                    add(t.id, node)
    return out


def _line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def regex_symbols(text, pairs):
    out = {}
    for rx, fmt in pairs:
        for m in rx.finditer(text):
            ln = _line_of(text, m.start())
            out.setdefault(fmt(m), (ln, ln, None))
    return out


def json_symbols(text):
    data = json.loads(text)
    entries = data if isinstance(data, list) else [data]
    vals = {}
    for e in entries:
        if isinstance(e, dict):
            for k, v in e.items():
                vals.setdefault(k, []).append(v)
    out = {}
    for k, v in vals.items():
        m = re.search(r'"%s"\s*:' % re.escape(k), text)
        ln = _line_of(text, m.start()) if m else 1
        out[k] = (ln, ln, _fp(json.dumps(v, sort_keys=True)))
    return out


def symbols(path, data):
    """{name: (line, end_line, fingerprint or None)}, or None when the type is not tracked or
    does not parse. A None fingerprint means presence only: changes inside are not seen."""
    if data is None:
        return {}
    try:
        text = data.decode()
        if path.endswith(".py"):
            return py_symbols(text)
        if path.endswith((".js", ".mjs")):
            # drop comments (routes.js keeps a commented-out draft API), keeping line numbers
            text = JS_BLOCK.sub(lambda m: "\n" * m.group(0).count("\n"), text)
            text = JS_LINE.sub(r"\1", text)
            return regex_symbols(text, [
                (JS_ROUTE, lambda m: "%s %s" % (m.group(1).upper(), m.group(2))),
                (JS_EXPORT, lambda m: m.group(1))])
        if path.endswith(".proto"):
            return regex_symbols(text, [(PROTO_DEF, lambda m: m.group(1))])
        if path.endswith(".json"):
            return json_symbols(text)
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return None
    return None


def symbol_at(syms, line):
    """The innermost symbol whose span holds line (methods win over their class)."""
    best = None
    for name, (lo, hi, _) in (syms or {}).items():
        if lo <= line <= hi and (best is None or lo >= syms[best][0]):
            best = name
    return best


def diff_symbols(old, new):
    """[(op, name, line)] with op '+' added, '-' removed, '~' changed. Line is the new one
    (the old one for removals)."""
    out = []
    for name in sorted(set(old) | set(new), key=lambda n: (new.get(n) or old.get(n))[0]):
        if name not in old:
            out.append(("+", name, new[name][0]))
        elif name not in new:
            out.append(("-", name, old[name][0]))
        elif old[name][2] is not None and old[name][2] != new[name][2]:
            out.append(("~", name, new[name][0]))
    return out


# --- sources -----------------------------------------------------------------------------------


class GhSource:
    """GitHub REST through the gh CLI. GET requests only."""

    def __init__(self, repo):
        self.repo, self.branch = repo["repo"], repo["branch"]
        self._cmp = {}

    def api(self, path, raw=False):
        cmd = ["gh", "api", "-X", "GET", "repos/%s/%s" % (self.repo, path)]
        if raw:
            cmd[2:2] = ["-H", "Accept: application/vnd.github.raw"]
        return run(cmd)

    def head(self):
        return json.loads(self.api("branches/" + urllib.parse.quote(self.branch, safe="")))[
            "commit"]["sha"]

    def resolve(self, rev):
        try:
            return json.loads(self.api("commits/" + urllib.parse.quote(rev, safe="")))["sha"]
        except SyncError as e:
            raise SyncError("%s: unknown revision %s (%s)" % (self.repo, rev, e)) from None

    def compare(self, base, head):
        if (base, head) not in self._cmp:
            self._cmp[base, head] = json.loads(self.api("compare/%s...%s?per_page=250"
                                                        % (base, head)))
        return self._cmp[base, head]

    def commits(self, base, head):
        c = self.compare(base, head)
        rows = [(x["sha"], (x["commit"]["author"] or {}).get("name", "?"),
                 (x["commit"]["author"] or {}).get("date", "")[:10],
                 x["commit"]["message"].splitlines()[0]) for x in c.get("commits", [])]
        return rows, c.get("total_commits", len(rows))

    def dropped(self, base, head):
        """Commits in base that head no longer has (Brent force-pushed)."""
        if self.compare(base, head).get("status") not in ("diverged", "behind"):
            return []
        return [r[0] for r in self.commits(head, base)[0]]

    def files(self, base, head):
        out = []
        for f in self.compare(base, head).get("files", []):
            st = {"added": "A", "removed": "D", "renamed": "R"}.get(f["status"], "M")
            out.append((st, f["filename"], f.get("previous_filename")))
        return out

    def truncated(self, base, head):
        return len(self.compare(base, head).get("files", [])) >= 300

    def show(self, sha, path):
        try:
            return self.api("contents/%s?ref=%s" % (urllib.parse.quote(path), sha), raw=True)
        except SyncError as e:
            if "404" in str(e) or "Not Found" in str(e):
                return None
            raise

    def ls(self, sha):
        tree = json.loads(self.api("git/trees/%s?recursive=1" % sha))
        return [t["path"] for t in tree.get("tree", []) if t["type"] == "blob"]


class GitSource:
    """A git directory: a local clone given with --repo-dir, or a cached partial clone."""

    def __init__(self, repo, gitdir, url=None):
        self.repo, self.branch, self.gitdir, self.url = repo["repo"], repo["branch"], gitdir, url

    def git(self, *args, ok=(0,)):
        return run(["git", "--git-dir" if self.url else "-C", self.gitdir] + list(args), ok=ok)

    @classmethod
    def cached(cls, repo, cache_dir, fetch=True):
        url = "https://github.com/%s.git" % repo["repo"]
        gitdir = os.path.join(cache_dir, repo["key"] + ".git")
        if not os.path.isdir(gitdir):
            os.makedirs(cache_dir, exist_ok=True)
            run(["git", "clone", "--bare", "--quiet", "--filter=blob:none", "--no-tags",
                 "--single-branch", "--branch", repo["branch"], url, gitdir])
        elif fetch:
            run(["git", "--git-dir", gitdir, "fetch", "--quiet", "--filter=blob:none", "--no-tags",
                 url, "+refs/heads/%s:refs/heads/%s" % (repo["branch"], repo["branch"])])
        return cls(repo, gitdir, url)

    def head(self):
        for ref in ("refs/remotes/origin/" + self.branch, "refs/heads/" + self.branch, "HEAD"):
            out = self.git("rev-parse", "--verify", "--quiet", ref + "^{commit}", ok=(0, 1))
            if out.strip():
                return out.decode().strip()
        raise SyncError("%s: no ref for branch %s" % (self.gitdir, self.branch))

    def resolve(self, rev):
        out = self.git("rev-parse", "--verify", "--quiet", rev + "^{commit}", ok=(0, 1)).strip()
        if not out and self.url and re.match(r"^[0-9a-f]{40}$", rev):
            self.git("fetch", "--quiet", "--filter=blob:none", "--no-tags", self.url, rev,
                     ok=(0, 1, 128))
            out = self.git("rev-parse", "--verify", "--quiet", rev + "^{commit}",
                           ok=(0, 1)).strip()
        if not out:
            raise SyncError("%s: unknown revision %s (force-pushed away? pass --since)"
                            % (self.repo, rev))
        return out.decode()

    def commits(self, base, head):
        out = self.git("log", "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=short",
                       "%s..%s" % (base, head)).decode()
        rows = [tuple(line.split("\x1f")) for line in out.splitlines() if line]
        return rows, len(rows)

    def dropped(self, base, head):
        out = self.git("rev-list", "%s..%s" % (head, base)).decode()
        return out.split()

    def files(self, base, head):
        out = self.git("diff", "--name-status", "--no-renames", "-z", base, head).decode()
        parts = out.split("\0")
        return [(parts[i][0], parts[i + 1], None) for i in range(0, len(parts) - 1, 2)]

    def truncated(self, base, head):
        return False

    def show(self, sha, path):
        spec = "%s:%s" % (sha, path)
        try:
            self.git("cat-file", "-e", spec)  # a partial clone fetches the blob here
        except SyncError:
            return None
        return self.git("cat-file", "blob", spec)

    def ls(self, sha):
        return self.git("ls-tree", "-r", "--name-only", "-z", sha).decode().split("\0")[:-1]


def open_source(repo, args):
    if repo["key"] in args.repo_dir:
        d = os.path.expanduser(args.repo_dir[repo["key"]])
        if not os.path.isdir(d):
            raise SyncError("--repo-dir %s=%s: no such directory" % (repo["key"], d))
        return GitSource(repo, d)
    via = args.via
    if via == "auto":
        via = "gh" if shutil.which("gh") else "git"
    if via == "gh":
        return GhSource(repo)
    return GitSource.cached(repo, args.cache, fetch=not args.no_fetch)


# --- report ------------------------------------------------------------------------------------


def describe(rule):
    if rule is None:
        return "UNMAPPED"
    if rule["kind"] == "skip":
        return "not ported: %s" % (rule.get("what") or rule["key"])
    s = "-> %s" % rule.get("ours", "?")
    if rule.get("status"):
        s += "  [%s]" % rule["status"]
    return s


def missing_targets(rule):
    if rule is None or rule["kind"] != "map":
        return []
    return [p for p in ours_paths(rule) if not os.path.exists(os.path.join(HERE, p))]


def our_citations():
    """[(our_file, our_line, cited_file, cited_line, sha)] from this repo's text files."""
    out = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (
            "history", "results", "media", "images", "data", "__pycache__")]
        for fn in files:
            if not fn.endswith((".py", ".md", ".toml")):
                continue
            p = os.path.join(root, fn)
            if os.path.samefile(p, DOC):
                continue
            try:
                with open(p) as f:
                    lines = f.read().splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(lines):
                for m in CITE.finditer(line):
                    out.append((os.path.relpath(p, HERE), i + 1, m.group(1), int(m.group(2)),
                                m.group(3)))
    return out


def check_repo(repo, rules, args, cites):
    """Compare one repo; returns a result dict (also used for --json and --record)."""
    src = open_source(repo, args)
    head = src.resolve(args.head.get(repo["key"])) if repo["key"] in args.head else src.head()
    base = src.resolve(args.since.get(repo["key"], repo["sha"]))
    res = {"key": repo["key"], "repo": repo["repo"], "branch": repo["branch"], "base": base,
           "head": head, "commits": [], "total": 0, "dropped": [], "files": [], "other": [],
           "unmapped": 0, "new_loose": 0, "missing": set(), "cites": [], "lines": {}}
    if base == head:
        return res, src
    res["commits"], res["total"] = src.commits(base, head)
    res["dropped"] = src.dropped(base, head)
    res["truncated"] = src.truncated(base, head)
    for st, path, old in src.files(base, head):
        if not watched(repo, path):
            res["other"].append((st, path))
            continue
        frule = lookup(rules, repo["key"], path)
        entry = {"status": st, "path": path, "rule": frule, "symbols": []}
        old_syms = symbols(path, src.show(base, old or path)) if st != "A" else {}
        new_syms = symbols(path, src.show(head, path)) if st != "D" else {}
        if old_syms is None or new_syms is None:
            entry["note"] = "not parsed; file-level only"
        else:
            for op, name, line in diff_symbols(old_syms, new_syms):
                key = "%s::%s" % (path, name)
                r = lookup(rules, repo["key"], key)
                entry["symbols"].append({"op": op, "name": name, "line": line, "rule": r})
                if r is None:
                    res["unmapped"] += 1
                elif op == "+" and loose(r, key):
                    res["new_loose"] += 1
                res["missing"].update(missing_targets(r))
            entry["old_syms"], entry["new_syms"] = old_syms, new_syms
        if frule is None and not entry["symbols"]:
            res["unmapped"] += 1
        res["missing"].update(missing_targets(frule))
        res["files"].append(entry)
    res["cites"] = stale_citations(repo, src, res, cites)
    return res, src


def stale_citations(repo, src, res, cites):
    """Our citations of files that changed in this sync, whose symbol changed or moved."""
    by_path = {e["path"]: e for e in res["files"] if "new_syms" in e}
    out = []
    for ours, oline, cfile, cline, csha in cites:
        hits = [p for p in by_path if p == cfile or p.endswith("/" + cfile)]
        if len(hits) != 1:
            continue
        e = by_path[hits[0]]
        old = e["old_syms"]
        if not res["base"].startswith(csha):
            try:
                old = symbols(e["path"], src.show(src.resolve(csha), e["path"])) or {}
            except SyncError:
                continue
        name = symbol_at(old, cline)
        if name is None:
            continue
        if name not in e["new_syms"]:
            out.append((ours, oline, cfile, cline, name, "removed upstream", None))
            continue
        start, nstart = old[name][0], e["new_syms"][name][0]
        changed = old[name][2] is not None and old[name][2] != e["new_syms"][name][2]
        if changed or nstart != start:
            # keep the cited offset into the symbol (exact when it only moved)
            out.append((ours, oline, cfile, cline, name, "changed" if changed else "moved",
                        nstart + (cline - start)))
    return out


def print_result(res, verbose):
    short = res["head"][:7]
    print("== %s  %s@%s" % (res["key"], res["repo"], res["branch"]))
    if res["base"] == res["head"]:
        print("   up to date at %s" % short)
        return
    print("   last synced %s, head %s: %d new commit(s)%s" % (
        res["base"][:7], short, res["total"],
        " (showing %d)" % len(res["commits"]) if res["total"] > len(res["commits"]) else ""))
    if res["dropped"]:
        print("   HISTORY REWRITTEN: %d synced commit(s) no longer on %s: %s" % (
            len(res["dropped"]), res["branch"], " ".join(c[:7] for c in res["dropped"][:10])))
    for sha, author, date, subject in res["commits"]:
        print("   %s %s %-10s %s" % (sha[:7], date, author[:10], subject[:60]))
    if res.get("truncated"):
        print("   WARNING: GitHub listed only the first 300 files; rerun with --via git")
    print("   watched files:" if res["files"] else "   no watched files changed")
    for e in res["files"]:
        flag = "" if e["rule"] or e["symbols"] else "  UNMAPPED"
        print("   %s %s  %s%s" % (e["status"], e["path"],
                                  describe(e["rule"]) if e["rule"] else "", flag))
        if e.get("note"):
            print("       (%s)" % e["note"])
        for s in e["symbols"]:
            mark = ""
            key = "%s::%s" % (e["path"], s["name"])
            if s["rule"] and s["op"] == "+" and loose(s["rule"], key):
                mark = "  NEW: matched by %r, confirm" % s["rule"]["key"]
            print("       %s %-36s :%-5d %s%s" % (s["op"], s["name"], s["line"],
                                                  describe(s["rule"]), mark))
    if res["other"]:
        print("   %d other file(s) outside the watch list%s" % (
            len(res["other"]), ":" if verbose else " (-v lists them)"))
        if verbose:
            for st, p in res["other"]:
                print("     %s %s" % (st, p))
    for ours, oline, cfile, cline, name, why, nline in res["cites"]:
        print("   citation %s:%d -> %s:%d (%s) %s%s" % (
            ours, oline, cfile, cline, name, why, "; now :%d @ %s" % (nline, short)
            if nline else ""))
    if res["missing"]:
        print("   mapped targets not in this tree yet: %s" % ", ".join(sorted(res["missing"])))


def print_symbols(repo, src, rules, sha):
    """Every tracked symbol at sha with its mapping. Returns the number unmapped."""
    unmapped = 0
    print("== %s  %s @ %s" % (repo["key"], repo["repo"], sha[:7]))
    for path in sorted(p for p in src.ls(sha) if watched(repo, p)):
        frule = lookup(rules, repo["key"], path)
        syms = symbols(path, src.show(sha, path))
        if not syms:
            if frule is None:
                unmapped += 1
            print("   %s  %s" % (path, describe(frule)))
            continue
        print("   %s" % path)
        for name, (line, _, _) in sorted(syms.items(), key=lambda kv: kv[1][0]):
            r = lookup(rules, repo["key"], "%s::%s" % (path, name))
            unmapped += r is None
            print("       %-40s :%-5d %s" % (name, line, describe(r)))
    return unmapped


# --- --record ----------------------------------------------------------------------------------


def record(text, repos, results, today):
    """New doc text with each repo's sha set to its head and the Line column of exact-symbol
    mapping rows moved to the head's line numbers."""
    lines = text.splitlines(keepends=True)
    tables = read_tables(text)

    def set_cell(row, col, value):
        """Rewrite one cell in place, keeping its padding and backticks."""
        i = row["_line"]
        cells = lines[i].rstrip("\n").strip().strip("|").split("|")
        k = [h for h in row if not h.startswith("_")].index(col)  # header order
        old = cells[k]
        tick = "`" if old.strip().startswith("`") else ""
        cells[k] = (" " * (len(old) - len(old.lstrip())) + tick + value + tick).ljust(len(old))
        lines[i] = "|" + "|".join(cells) + "|\n"

    for r in tables[SEC_SYNC]:
        res = results.get(r["key"])
        if res is None:
            continue
        set_cell(r, "sha", res["head"][:7])
        if "synced" in r:
            set_cell(r, "synced", today)
    for r in tables[SEC_MAP]:
        res = results.get(r.get("repo"))
        key = r.get("brent", "")
        if res is None or "line" not in r or "::" not in key or any(c in key for c in "*?["):
            continue
        path, _, name = key.partition("::")
        line = res["lines"].get((path, name))
        if line:
            set_cell(r, "line", str(line))
    return "".join(lines)


def head_lines(src, repo, rules, sha):
    """{(path, symbol): line} at sha for the exact-symbol rows of this repo."""
    want = {}
    for r in rules:
        exact = "::" in r["key"] and not any(c in r["key"] for c in "*?[")
        if r["repo"] == repo["key"] and exact:
            path, _, name = r["key"].partition("::")
            want.setdefault(path, set()).add(name)
    out = {}
    for path, names in want.items():
        syms = symbols(path, src.show(sha, path)) or {}
        for n in names:
            if n in syms:
                out[path, n] = syms[n][0]
    return out


# --- main --------------------------------------------------------------------------------------


def _pairs(values, flag):
    out = {}
    for v in values or []:
        k, sep, val = v.partition("=")
        if not sep:
            raise SystemExit("%s wants KEY=VALUE, got %r" % (flag, v))
        out[k] = val
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n", 2)[2])
    ap.add_argument("repos", nargs="*", help="keys from the '%s' table (default: all)" % SEC_SYNC)
    ap.add_argument("--doc", default=DOC, help="sync record (default docs/BRENT-SYNC.md)")
    ap.add_argument("--via", choices=("auto", "gh", "git"), default="auto",
                    help="gh api (default when gh is installed) or a cached partial clone")
    ap.add_argument("--cache", default=CACHE, help="where --via git keeps its clones")
    ap.add_argument("--no-fetch", action="store_true", help="--via git: use the cache as is")
    ap.add_argument("--repo-dir", action="append", metavar="KEY=DIR",
                    help="use this local clone for KEY; nothing is fetched")
    ap.add_argument("--since", action="append", metavar="KEY=REV",
                    help="compare from REV instead of the recorded sha")
    ap.add_argument("--head", action="append", metavar="KEY=REV",
                    help="compare to REV instead of the branch head")
    ap.add_argument("--symbols", action="store_true",
                    help="list every tracked symbol at the head with its mapping")
    ap.add_argument("--record", action="store_true",
                    help="write the heads (and moved line numbers) into the doc after review")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("-v", "--verbose", action="store_true", help="list unwatched files too")
    args = ap.parse_args(argv)
    args.repo_dir = _pairs(args.repo_dir, "--repo-dir")
    args.since = _pairs(args.since, "--since")
    args.head = _pairs(args.head, "--head")

    try:
        text, repos, rules = load_doc(args.doc)
    except (OSError, SyncError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    keys = args.repos or list(repos)
    for k in keys:
        if k not in repos:
            print("error: no repo %r in %s (have: %s)" % (k, args.doc, ", ".join(repos)),
                  file=sys.stderr)
            return 1

    if args.symbols:
        unmapped, err = 0, False
        for k in keys:
            try:
                src = open_source(repos[k], args)
                sha = src.resolve(args.head[k]) if k in args.head else src.head()
                unmapped += print_symbols(repos[k], src, rules, sha)
            except SyncError as e:
                print("error: %s: %s" % (k, e), file=sys.stderr)
                err = True
        print("%d unmapped" % unmapped)
        return 1 if err else 3 if unmapped else 0

    cites = our_citations()
    results, err, changed, unmapped = {}, False, False, 0
    for k in keys:
        try:
            res, src = check_repo(repos[k], rules, args, cites)
            if args.record and res["base"] != res["head"]:
                res["lines"] = head_lines(src, repos[k], rules, res["head"])
        except SyncError as e:
            print("error: %s: %s" % (k, e), file=sys.stderr)
            err = True
            continue
        results[k] = res
        changed |= res["base"] != res["head"]
        unmapped += res["unmapped"]
        if not args.json:
            print_result(res, args.verbose)

    if args.json:
        def clean(e):
            return {"status": e["status"], "path": e["path"],
                    "rule": e["rule"] and e["rule"]["key"], "note": e.get("note"),
                    "symbols": [dict(s, rule=s["rule"] and s["rule"]["key"])
                                for s in e["symbols"]]}
        out = {k: dict({x: r[x] for x in ("repo", "branch", "base", "head", "total", "dropped",
                                          "other", "unmapped", "new_loose", "cites")},
                       commits=[list(c) for c in r["commits"]],
                       files=[clean(e) for e in r["files"]], missing=sorted(r["missing"]))
               for k, r in results.items()}
        json.dump(out, sys.stdout, indent=1)
        print()
    elif not err:
        print("%s: %d unmapped" % ("changes upstream" if changed else "in sync", unmapped))

    if args.record and results:
        if err or args.since or args.head:
            print("not recording: %s" % ("an error above" if err else "--since/--head given"),
                  file=sys.stderr)
        else:
            today = datetime.date.today().isoformat()
            new = record(text, repos, {k: r for k, r in results.items()
                                       if r["base"] != r["head"]}, today)
            if new != text:
                with open(args.doc, "w") as f:
                    f.write(new)
                print("recorded new heads in %s" % args.doc)
    if err:
        return 1
    return 3 if unmapped else 2 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
