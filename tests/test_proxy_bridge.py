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


def test_session_renew_and_vm_wake_share_proxy_without_secrets():
    """HTTP renewal, VM wake, and confirmation must all keep the same proxy."""
    def response(state):
        return SimpleNamespace(status_code=200, cookies=[],
                               json=lambda: {"status": "assigned",
                                             "vm_id": "vm-fixture", "vm_state": state})
    with (patch("requests.get", side_effect=[response("SUSPENDED"), response("RUNNING")]) as get,
          patch("requests.post", return_value=SimpleNamespace(status_code=200)) as post):
        result = MuseEngine.renew_session_http({"hatch_sess": "dummy"},
                                              proxy_server="http://127.0.0.1:32123")
    assert result["wake_ok"] is True
    for call in get.call_args_list + post.call_args_list:
        assert call.kwargs["proxies"] == {
            "http": "http://127.0.0.1:32123",
            "https": "http://127.0.0.1:32123",
        }


def test_admin_proxy_save_redacted_and_busy_change_rejected(tmp_path):
    import app
    from config import Config
    from fastapi import HTTPException
    cfg = Config(base_dir=str(tmp_path))
    cfg.ensure_dirs()
    isolated = Store(cfg)
    acc = isolated.add_account({"hatch_sess": "fixture"}, label="test")
    proxy = "socks5://admin:topsecret@127.0.0.1:1080"
    with (patch.object(app, "store", isolated),
          patch.object(app.ACCOUNT_POOL, "store", isolated)):
        result = app.patch_account(acc["id"], app.AccountPatch(proxy_url=proxy))
        assert result["has_proxy"] is True
        assert "proxy_url" not in result
        assert "topsecret" not in str(result)
        assert isolated.get_account(acc["id"])["proxy_url"] == proxy
        assert (tmp_path / "data" / "accounts.json").stat().st_mode & 0o077 == 0

        with app.ACCOUNT_POOL.cond:
            app.ACCOUNT_POOL.busy_accounts.add(acc["id"])
        try:
            with pytest.raises(HTTPException) as error:
                app.patch_account(acc["id"], app.AccountPatch(proxy_url=""))
            assert error.value.status_code == 409
            assert isolated.get_account(acc["id"])["proxy_url"] == proxy
        finally:
            with app.ACCOUNT_POOL.cond:
                app.ACCOUNT_POOL.busy_accounts.discard(acc["id"])
        result = app.patch_account(acc["id"], app.AccountPatch(proxy_url=""))
        assert result["has_proxy"] is False
        assert isolated.get_account(acc["id"])["proxy_url"] == ""


class FakeHTTPNoAuthProxy(socketserver.BaseRequestHandler):
    """Fake unauthenticated HTTP proxy: reject unexpected Basic credentials."""
    def handle(self):
        header, tail = head(self.request)
        self.server.requests.append(header)
        if b"Proxy-Authorization:" in header:
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        if header.startswith(b"CONNECT "):
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            content = tail or self.request.recv(1024)
            self.server.payloads.append(content)
            self.request.sendall(b"reply")
        else:
            self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nPONG")


class FakeSOCKSNoAuthProxy(socketserver.BaseRequestHandler):
    """SOCKS5 must negotiate method 0x00 and not send username/password."""
    def handle(self):
        s = self.request
        assert exact(s, 3) == b"\x05\x01\x00"
        s.sendall(b"\x05\x00")
        start = exact(s, 4)
        assert start[:3] == b"\x05\x01\x00"
        if start[3] == 3:
            hostname = exact(s, exact(s, 1)[0])
        elif start[3] == 1:
            hostname = exact(s, 4)
        else:
            hostname = exact(s, 16)
        port = int.from_bytes(exact(s, 2), "big")
        self.server.destinations.append((hostname, port))
        s.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
        h, _ = head(s)
        self.server.requests.append(h)
        s.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nPONG")


def test_noauth_proxy_url_accepts_empty_credentials_but_not_partial():
    for scheme in ("http", "socks5"):
        url = f"{scheme}://127.0.0.1:20240"
        parsed = parse_proxy(url)
        assert parsed.scheme == scheme
        assert parsed.host == "127.0.0.1"
        assert parsed.port == 20240
        assert parsed.username == "" and parsed.password == ""
        empty = parse_proxy(f"{scheme}://:@127.0.0.1:20240")
        assert empty.username == "" and empty.password == ""
        for invalid in (f"{scheme}://user@127.0.0.1:20240",
                        f"{scheme}://user:@127.0.0.1:20240",
                        f"{scheme}://:password@127.0.0.1:20240"):
            with pytest.raises(ProxyConfigError):
                parse_proxy(invalid)


def test_noauth_http_proxy_get_and_https_connect():
    with upstream(FakeHTTPNoAuthProxy) as server:
        manager = ProxyBridgeManager()
        try:
            local = manager.local_url(
                "local-http", f"http://127.0.0.1:{server.server_address[1]}")
            response = requests.get(
                "http://example.invalid/noauth", proxies={"http": local}, timeout=5)
            assert response.status_code == 200 and response.text == "PONG"
            port = int(local.rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                client.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
                assert client.recv(4096).startswith(b"HTTP/1.1 200")
                client.sendall(b"hello")
                assert client.recv(4096) == b"reply"
            assert len(server.requests) == 2
            assert all(b"Proxy-Authorization:" not in h for h in server.requests)
            assert server.payloads == [b"hello"]
        finally:
            manager.close_all()


def test_noauth_socks5_proxy_uses_method_zero():
    with upstream(FakeSOCKSNoAuthProxy) as server:
        manager = ProxyBridgeManager()
        try:
            local = manager.local_url(
                "local-socks", f"socks5://127.0.0.1:{server.server_address[1]}")
            response = requests.get(
                "http://example.test/noauth", proxies={"http": local}, timeout=5)
            assert response.status_code == 200 and response.text == "PONG"
            assert server.destinations == [(b"example.test", 80)]
            assert server.requests[0].startswith(b"GET /noauth HTTP/1.1")
        finally:
            manager.close_all()


def test_admin_proxy_test_uses_cloudflare_and_ignores_environment(tmp_path):
    from unittest.mock import MagicMock
    import app
    from config import Config
    cfg = Config(base_dir=str(tmp_path))
    cfg.ensure_dirs()
    store = Store(cfg)
    account = store.add_account(
        {"hatch_sess": "fixture"}, "test",
        proxy_url="http://127.0.0.1:20240",
    )
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value.text = "fl=22f1\nh=www.cloudflare.com\nip=203.0.113.15\n"
    with (patch.object(app, "store", store),
          patch.object(app.engine, "proxy_for_account",
                       return_value=("http://127.0.0.1:20240", "http://127.0.0.1:23456")),
          patch("requests.Session", return_value=session)):
        result = app.test_account_proxy(account["id"])
    assert result == {"ok": True, "ip": "203.0.113.15"}
    assert session.trust_env is False
    session.get.assert_called_once_with(
        "https://www.cloudflare.com/cdn-cgi/trace",
        timeout=15,
        proxies={"http": "http://127.0.0.1:23456", "https": "http://127.0.0.1:23456"},
        allow_redirects=False,
    )


def test_admin_proxy_test_rejects_invalid_trace_without_returning_payload(tmp_path):
    from unittest.mock import MagicMock
    import app
    from fastapi import HTTPException
    from config import Config
    cfg = Config(base_dir=str(tmp_path))
    cfg.ensure_dirs()
    store = Store(cfg)
    account = store.add_account(
        {"hatch_sess": "fixture"}, "test",
        proxy_url="socks5://127.0.0.1:65220",
    )
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value.text = "invalid output with secret fixture"
    with (patch.object(app, "store", store),
          patch.object(app.engine, "proxy_for_account",
                       return_value=("socks5://127.0.0.1:65220", "http://127.0.0.1:23457")),
          patch("requests.Session", return_value=session)):
        with pytest.raises(HTTPException) as err:
            app.test_account_proxy(account["id"])
    assert err.value.status_code == 502
    assert "secret fixture" not in err.value.detail
