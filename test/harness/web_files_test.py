# SPDX-FileCopyrightText: 2026 Mikey Sklar
# SPDX-License-Identifier: MIT-0
"""WebFs and Board.web() against a fake web workflow on 127.0.0.1, with the status codes of
supervisor/shared/web_workflow/web_workflow.c (circuitpython 30d7addb4e): PUT 201 new / 204
existing, mkdir 201 / 204 when it exists, GET 404 missing, DELETE 204 / 404, 401 bad auth,
409 while a USB host holds the drive."""
import base64
import json
import threading
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import farmlib

pytestmark = pytest.mark.smoke


class FakeWebWorkflow:
    def __init__(self, board_id="adafruit_feather_esp32c6_4mbflash_nopsram",
                 password=farmlib.WEB_API_PASSWORD):
        self.files, self.dirs = {}, {""}
        self.board_id, self.held = board_id, False
        want = "Basic " + base64.b64encode((":" + password).encode()).decode()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # pylint: disable=arguments-differ
                pass

            def _reply(self, code, body=b""):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _fs(self):
                if self.path == "/cp/version.json":
                    return None
                if self.path == "/fs/" and self.headers.get("Authorization") == want:
                    self._reply(200, b'{"files": []}')
                    return False
                if self.headers.get("Authorization") != want:
                    self._reply(401)
                    return False
                return urllib.parse.unquote(self.path[len("/fs/"):])

            def do_GET(self):  # pylint: disable=invalid-name
                rel = self._fs()
                if rel is None:
                    self._reply(200, json.dumps({"board_id": fake.board_id}).encode())
                elif rel is not False:
                    self._reply(200, fake.files[rel]) if rel in fake.files else self._reply(404)

            def do_PUT(self):  # pylint: disable=invalid-name
                rel = self._fs()
                if rel is False:
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if fake.held:
                    return self._reply(409)
                if rel.endswith("/"):
                    d = rel.rstrip("/")
                    parts = d.split("/")
                    new = d not in fake.dirs
                    fake.dirs.update("/".join(parts[:i + 1]) for i in range(len(parts)))
                    return self._reply(201 if new else 204)
                if rel.rpartition("/")[0] not in fake.dirs:
                    return self._reply(404)
                code = 204 if rel in fake.files else 201
                fake.files[rel] = body
                return self._reply(code)

            def do_DELETE(self):  # pylint: disable=invalid-name
                rel = self._fs()
                if rel is not False:
                    self._reply(204) if fake.files.pop(rel, None) is not None else self._reply(404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def ww():
    f = FakeWebWorkflow()
    yield f
    f.close()


BOARD = types.SimpleNamespace(name="fake_c6")


def test_put_get_delete(ww):
    w = farmlib.WebFs(BOARD, ww.base)
    w.put("lib/sub/x.py", "print(1)\n")
    assert ww.files["lib/sub/x.py"] == b"print(1)\n"
    assert {"lib", "lib/sub"} <= ww.dirs
    w.put("lib/sub/x.py", b"\x00\xff")  # existing file: 204
    assert w.get("lib/sub/x.py") == b"\x00\xff"
    w.delete("lib/sub/x.py")
    assert w.get("lib/sub/x.py") is None
    w.delete("lib/sub/x.py")  # missing: 404 is fine


def test_name_with_space_is_quoted(ww):
    farmlib.WebFs(BOARD, ww.base).put("my file.txt", b"x")
    assert ww.files["my file.txt"] == b"x"


def test_drive_held_by_usb_is_a_clear_error(ww):
    ww.held = True
    with pytest.raises(farmlib.FarmError, match="HTTP 409 .a USB host holds the drive"):
        farmlib.WebFs(BOARD, ww.base).put("code.py", b"x")


def test_wrong_password(ww):
    with pytest.raises(farmlib.FarmError, match="HTTP 401"):
        farmlib.WebFs(BOARD, ww.base, password="nope").get("code.py")


def _board(monkeypatch, ww, board_id, serial_mac="9C:9E:6E:5B:86:4C", host_mac="9c9e6e5b864c"):
    b = farmlib.Board({"name": "fake_c6", "board_id": board_id, "usb": "3-6.3.1", "fs": "web"}, {})
    monkeypatch.setattr(farmlib, "_read", lambda path, default=None: serial_mac
                        if path.endswith("/serial") else default)
    port = ww.base.rsplit(":", 1)[1]
    monkeypatch.setattr(farmlib, "_avahi_circuitpython", lambda timeout=8: [
        ("cpy-other-aabbccddeeff.local", "127.0.0.2", "80"),
        ("cpy-esp32c6_4mbflash_nopsram-%s.local" % host_mac, "127.0.0.1", port)])
    monkeypatch.setattr(b, "repl", lambda iface=0: pytest.fail("mDNS matched, no REPL needed"))
    return b


def test_board_web_finds_itself_by_mac_and_checks_board_id(ww, monkeypatch):
    b = _board(monkeypatch, ww, ww.board_id)
    assert b.mac() == "9c9e6e5b864c"
    w = b.web()
    assert w is not None and w.base == ww.base
    b.write_file("code.py", "print(3)\n")
    assert ww.files["code.py"] == b"print(3)\n"
    assert b.read_file("code.py") == "print(3)\n"


def test_board_web_refuses_another_board_id(ww, monkeypatch):
    b = _board(monkeypatch, ww, "adafruit_metro_esp32s3")
    monkeypatch.setattr(b, "repl", lambda iface=0: (_ for _ in ()).throw(farmlib.FarmError("x")))
    assert b.web() is None


def test_board_web_with_another_password_falls_back(ww, monkeypatch):
    """Before flashed_fs writes settings.toml the board has some other web password: web()
    says no, and the file methods use the REPL."""
    b = _board(monkeypatch, ww, ww.board_id)
    assert b.web(password="not-the-boards") is None
