"""P1 offline proxy tests: no Muse credentials, no external network."""
import base64
import socket
import socketserver
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from engine import MuseEngine
from proxy_bridge import ProxyBridgeManager, ProxyConfigError, parse_proxy
from store import Store


def exact(sock, count):
    data = b""
    while len(data) < count:
        item = sock.recv(count - len(data))
        if not item:
            raise OSError("closed")
        data += item
    return data


def head(sock):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        data.extend(sock.recv(4096))
    return bytes(data).split(b"\r\n\r\n", 1)


class FakeHTTPProxy(socketserver.BaseRequestHandler):
    def handle(self):
        header, tail = head(self.request)
        self.server.requests.append(header)
        if b"Proxy-Authorization: Basic " not in header:
            self.request.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            return
        if header.startswith(b"CONNECT "):
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            content = tail or self.request.recv(1024)
            self.server.payloads.append(content)
            self.request.sendall(b"reply")
        else:
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nPONG")


class FakeSOCKSProxy(socketserver.BaseRequestHandler):
    def handle(self):
        s = self.request
        assert exact(s, 3) == b"\x05\x01\x02"
        s.sendall(b"\x05\x02")
        ver, n = exact(s, 2)
        assert ver == 1
        user = exact(s, n)
        m = exact(s, 1)[0]
        password = exact(s, m)
        self.server.credentials.append((user, password))
        if password != b"secret":
            s.sendall(b"\x01\x01")
            return
        s.sendall(b"\x01\x00")
        start = exact(s, 4)
        assert start[:3] == b"\x05\x01\x00"
        if start[3] == 3:
            host = exact(s, exact(s, 1)[0])
        elif start[3] == 1:
            host = exact(s, 4)
        else:
            host = exact(s, 16)
        port = int.from_bytes(exact(s, 2), "big")
        self.server.destinations.append((host, port))
        s.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
        h, _ = head(s)
        self.server.requests.append(h)
        s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nPONG")


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextmanager
def upstream(handler):
    server = Server(("127.0.0.1", 0), handler)
    server.requests = []
    server.payloads = []
    server.credentials = []
    server.destinations = []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_validate_proxy_url_never_exposes_password():
    value = parse_proxy("http://hello%40world:p%3Ass@127.0.0.1:8080")
    assert value.username == "hello@world"
    assert value.password == "p:ss"
    for unsafe in (
        "http://user:SECRET@localhost",
        "http://user:SECRET@localhost:0",
        "https://user:SECRET@localhost:8080",
        "http://user:SECRET@localhost:8080/admin",
        "socks5://user@localhost:1080",
    ):
        with pytest.raises(ProxyConfigError) as err:
            parse_proxy(unsafe)
        assert "SECRET" not in str(err.value)


def test_http_proxy_basic_auth_and_account_ports():
    with upstream(FakeHTTPProxy) as server:
        manager = ProxyBridgeManager()
        try:
            auth_url = f"http://alice:password@127.0.0.1:{server.server_address[1]}"
            other_url = f"http://bob:password@127.0.0.1:{server.server_address[1]}"
            a = manager.local_url("account-a", auth_url)
            b = manager.local_url("account-b", other_url)
            assert a != b
            assert manager.local_url("account-a", auth_url) == a
            response = requests.get("http://example.invalid/test", proxies={"http": a},
                                    timeout=5)
            assert response.status_code == 200
            assert response.text == "PONG"
            assert server.requests
            assert b"Proxy-Authorization: Basic " in server.requests[0]
            assert base64.b64encode(b"alice:password") in server.requests[0]
            assert b"example.invalid/test" in server.requests[0]
        finally:
            manager.close_all()


def test_http_proxy_connect_tunnels_with_auth():
    with upstream(FakeHTTPProxy) as server:
        manager = ProxyBridgeManager()
        try:
            local = manager.local_url("acct", f"http://user:pass@127.0.0.1:{server.server_address[1]}")
            port = int(local.rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
                response = client.recv(4096)
                assert response.startswith(b"HTTP/1.1 200")
                client.sendall(b"hello")
                assert client.recv(4096) == b"reply"
            assert server.requests[0].startswith(b"CONNECT example.test:443")
            assert base64.b64encode(b"user:pass") in server.requests[0]
            assert server.payloads == [b"hello"]
        finally:
            manager.close_all()


def test_socks5_proxy_auth_and_destination():
    with upstream(FakeSOCKSProxy) as server:
        manager = ProxyBridgeManager()
        try:
            local = manager.local_url("socks-a", f"socks5://user:secret@127.0.0.1:{server.server_address[1]}")
            response = requests.get("http://example.test/socks", proxies={"http": local}, timeout=5)
            assert response.status_code == 200
            assert response.text == "PONG"
            assert server.credentials == [(b"user", b"secret")]
            assert server.destinations == [(b"example.test", 80)]
            assert server.requests[0].startswith(b"GET /socks HTTP/1.1")
        finally:
            manager.close_all()


def test_socks5_invalid_auth_fails_closed():
    with upstream(FakeSOCKSProxy) as server:
        manager = ProxyBridgeManager()
        try:
            local = manager.local_url("bad", f"socks5://user:wrong@127.0.0.1:{server.server_address[1]}")
            res = requests.get("http://example.test/socks", proxies={"http": local}, timeout=5)
            assert res.status_code == 502
        finally:
            manager.close_all()


def test_unavailable_proxy_returns_502_without_direct_request():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        unused_port = s.getsockname()[1]
    manager = ProxyBridgeManager()
    try:
        local = manager.local_url("broken", f"http://test:secret@127.0.0.1:{unused_port}")
        response = requests.get("http://example.test/path", proxies={"http": local}, timeout=5)
        assert response.status_code == 502
    finally:
        manager.close_all()


def test_account_proxy_not_exposed_by_store(tmp_path):
    from config import Config
    cfg = Config(base_dir=str(tmp_path))
    cfg.ensure_dirs()
    store = Store(cfg)
    secret = "http://name:verysecret@127.0.0.1:8080"
    acc = store.add_account({"hatch_sess": "dummy"}, "A", proxy_url=secret)
    public = store.list_accounts()[0]
    assert public["has_proxy"] is True
    assert "proxy_url" not in public
    assert "verysecret" not in str(public)
    assert store.get_account(acc["id"])["proxy_url"] == secret
    assert (tmp_path / "data" / "accounts.json").stat().st_mode & 0o077 == 0


def test_engine_browser_context_includes_local_proxy(tmp_path):
    cfg = SimpleNamespace(profile_dir=str(tmp_path / "profile"),
                          download_dir=str(tmp_path / "downloads"), cdp_port=9123)
    engine = MuseEngine(cfg)
    with upstream(FakeHTTPProxy) as server:
        engine.proxy_resolver = lambda aid: (
            f"http://u:pass@127.0.0.1:{server.server_address[1]}" if aid == "a" else "")
        try:
            configured, local = engine.proxy_for_account("a")
            assert configured.startswith("http://u:pass@")
            assert local.startswith("http://127.0.0.1:")
            class Browser:
                def __init__(self):
                    self.calls = []
                def send(self, method, args=None):
                    self.calls.append((method, args))
                    if method == "Target.createBrowserContext":
                        return {"result": {"browserContextId": "ctx"}}
                    if method == "Target.createTarget":
                        return {"result": {"targetId": "target"}}
                    if method == "Target.getTargetInfo":
                        return {"result": {"targetInfo": {"targetId": "target", "browserContextId": "ctx"}}}
                    return {"result": {}}
            engine.browser = Browser()
            page = Mock()
            page.send.return_value = {"result": {}}
            with patch("engine.CDP", return_value=page):
                engine._open_page(proxy_server=local)
            methods = dict(engine.browser.calls)
            assert methods["Target.createBrowserContext"] == {"proxyServer": local}
            assert "pass" not in str(engine.browser.calls)
        finally:
            engine.proxy_bridges.close_all()
