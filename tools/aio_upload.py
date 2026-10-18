#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""Post a farm run's summary to Mikey's own Adafruit IO account, from the host. Boards never
hold the key or talk to IO; the prod guard (test/fixtures/secrets.py) still refuses any
AIO-looking variable in a test run's environment, so the key lives in its own file.

  tools/aio_upload.py --junit results/overnight-X/brent.xml results/overnight-X/smoke.xml
  tools/aio_upload.py --junit run.xml --metric wifi_join.reconnect_ms@metro_esp32s3 --send

Without --send it only prints what it would post. Credentials come from ~/.config/cpr-aio.env
(override with --creds), KEY=VALUE lines, mode 600:
  CPR_AIO_USERNAME=mikeysklar
  CPR_AIO_KEY=...
Any username but ALLOWED_USERNAME is refused: farm data never goes to another account
(Brent's in particular).

Feeds, in group "cp-regress" (created on first post, Create Group Data in the IO HTTP API:
POST /api/v2/{username}/groups/{group}/data; missing feeds are created):
  passed, failed, errors           totals over the given junit files
  fail-<board>                     failed + errors on each board in the run
  <metric>-<board>                 each --metric, its value from history/metrics.jsonl this run
A free account has 10 feeds and 30 data points a minute; one run is one call, and more than
--max-feeds feeds is refused rather than trimmed.
"""
import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
from hil_report import farm_boards, read_junit  # noqa: E402  pylint: disable=wrong-import-position

ALLOWED_USERNAME = "mikeysklar"
GROUP = "cp-regress"
API = "https://io.adafruit.com/api/v2/%s/groups/%s/data"
DEFAULT_CREDS = os.path.expanduser("~/.config/cpr-aio.env")
METRICS = os.path.join(HERE, "history", "metrics.jsonl")


def feed_key(*parts):
    """IO feed keys: lowercase letters, digits and dashes."""
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")


def read_creds(path):
    """{name: value} from KEY=VALUE lines. Refuses a file others can read."""
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit("%s is readable by others; chmod 600 it" % path)
    creds = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip().strip("\"'")
    return creds


def check_account(username):
    """Reasons not to post, empty when it may."""
    if not username:
        return ["no CPR_AIO_USERNAME"]
    if username != ALLOWED_USERNAME:
        return ["Adafruit IO user %r is not %r; farm data only goes to Mikey's account"
                % (username, ALLOWED_USERNAME)]
    return []


def run_start(paths):
    """Earliest testsuite timestamp ("YYYY-MM-DDTHH:MM:SS...") over the junit files."""
    stamps = []
    for p in paths:
        for suite in ET.parse(p).getroot().iter("testsuite"):
            if suite.get("timestamp"):
                stamps.append(suite.get("timestamp")[:19])
    return min(stamps) if stamps else None


def summary(paths, boards):
    """[(feed key, value)] for the totals and per-board failures."""
    cases = [c for p in paths for c in read_junit(p, boards)]
    # DNF counts as failed here: the free tier's 10 feeds have no room for its own. DNS is a
    # board's planned exclusion, like a skip, and is not sent.
    count = {k: sum(c["outcome"] == k for c in cases) for k in ("passed", "failed", "error")}
    count["failed"] += sum(c["outcome"] == "dnf" for c in cases)
    feeds = [("passed", count["passed"]), ("failed", count["failed"]),
             ("errors", count["error"])]
    seen = sorted({c["board"] for c in cases if c["board"] != "host"})
    for b in seen:
        bad = sum(c["board"] == b and c["outcome"] in ("failed", "error", "dnf") for c in cases)
        feeds.append((feed_key("fail", b), bad))
    return feeds


def metric_values(specs, since, path=METRICS):
    """[(feed key, value)] for each "metric@board": its latest record at or after `since`.
    Returns (feeds, missing specs)."""
    want = {}
    for s in specs:
        metric, _, board = s.partition("@")
        if not board:
            raise SystemExit("--metric %r: expected metric@board" % s)
        want[(metric, board)] = None
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                k = (r.get("metric"), r.get("board"))
                if k in want and (since is None or r.get("ts", "") >= since):
                    want[k] = r["value"]
    except OSError:
        pass
    feeds = [(feed_key(m, b), v) for (m, b), v in want.items() if v is not None]
    missing = ["%s@%s" % k for k, v in want.items() if v is None]
    return feeds, missing


def post(username, key, feeds, created_at=None, opener=urllib.request.urlopen):
    body = {"feeds": [{"key": k, "value": v} for k, v in feeds]}
    if created_at:
        body["created_at"] = created_at
    req = urllib.request.Request(API % (username, GROUP), data=json.dumps(body).encode(),
                                 headers={"X-AIO-Key": key,
                                          "Content-Type": "application/json"},
                                 method="POST")
    with opener(req, timeout=30) as r:
        return r.status


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--junit", nargs="+", required=True)
    ap.add_argument("--metric", action="append", default=[], metavar="METRIC@BOARD")
    ap.add_argument("--farm", default=os.path.join(HERE, "farm.toml"))
    ap.add_argument("--metrics-file", default=METRICS)
    ap.add_argument("--creds", default=DEFAULT_CREDS)
    ap.add_argument("--max-feeds", type=int, default=10)
    ap.add_argument("--send", action="store_true", help="post; without it, print only")
    a = ap.parse_args(argv)

    paths = [p for p in a.junit if os.path.isfile(p)]
    if not paths:
        raise SystemExit("no junit file among %s" % a.junit)
    since = run_start(paths)
    feeds = summary(paths, farm_boards(a.farm))
    extra, missing = metric_values(a.metric, since, a.metrics_file)
    feeds += extra
    for m in missing:
        print("metric %s: no value since %s, not sent" % (m, since))
    if len(feeds) > a.max_feeds:
        raise SystemExit("%d feeds, more than --max-feeds %d; drop a --metric"
                         % (len(feeds), a.max_feeds))

    print("group %s, run started %s:" % (GROUP, since))
    for k, v in feeds:
        print("  %-40s %s" % (k, v))
    if not a.send:
        print("(not sent: no --send)")
        return 0

    if not os.path.isfile(a.creds):
        raise SystemExit("no %s; nothing sent" % a.creds)
    creds = read_creds(a.creds)
    username = creds.get("CPR_AIO_USERNAME", "")
    why = check_account(username)
    if why:
        raise SystemExit("refusing to post: " + "; ".join(why))
    if not creds.get("CPR_AIO_KEY"):
        raise SystemExit("no CPR_AIO_KEY in %s" % a.creds)
    try:
        status = post(username, creds["CPR_AIO_KEY"], feeds)
    except urllib.error.HTTPError as e:
        raise SystemExit("Adafruit IO refused the post: HTTP %d %s" % (e.code, e.reason))
    except urllib.error.URLError as e:
        raise SystemExit("could not reach Adafruit IO: %s" % e.reason)
    print("posted %d feeds to %s/%s (HTTP %d)" % (len(feeds), username, GROUP, status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
