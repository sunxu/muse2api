"""每账号独立的本地 HTTP 代理转发器（HTTP Basic / SOCKS5 用户名密码）。

BrowserContext 连接 127.0.0.1 的无认证 HTTP 代理；本模块独立完成上游认证。
本地监听仅绑定 loopback。任何上游异常都返回错误，绝不直接访问目标站。
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import select
import socket
import socketserver
import threading
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

log = logging.getLogger("muse2api")


class ProxyConfigError(ValueError):
    """配置错误；异常文字严禁包含原始凭据。"""


def parse_proxy(value: str):
    if not isinstance(value, str) or len(value) > 2048 or not value:
        raise ProxyConfigError("代理地址不能为空或过长")
    if any(ch in value for ch in ("\r", "\n", "\t", "\x00")):
        raise ProxyConfigError("代理地址包含非法控制字符")
    try:
        url = urlsplit(value)
        if url.scheme.lower() not in ("http", "socks5"):
            raise ValueError()
        if not url.hostname or not url.port or not 1 <= url.port <= 65535:
            raise ValueError()
        if url.path not in ("", "/") or url.query or url.fragment:
            raise ValueError()
        # Both omitted => no authentication. Partial credentials are never valid.
        if url.username is None and url.password is None:
            username, password = "", ""
        elif url.username is not None and url.password is not None:
            username, password = unquote(url.username), unquote(url.password)
            if bool(username) != bool(password):
                raise ValueError()
        else:
            raise ValueError()
        if any(ord(c) < 32 or ord(c) == 127 for c in username + password):
            raise ValueError()
        if url.scheme.lower() == "socks5" and (len(username.encode()) > 255 or len(password.encode()) > 255):
            raise ValueError()
        return ProxySpec(url.scheme.lower(), url.hostname, url.port, username, password)
    except (ValueError, TypeError) as exc:
        raise ProxyConfigError("代理格式错误：使用 http(socks5)://主机:端口 或 http(socks5)://用户:密码@主机:端口") from None


@dataclass(frozen=True)
class ProxySpec:
    scheme: str
    host: str
    port: int
    username: str
    password: str


def _read_headers(sock: socket.socket, limit: int = 65536):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("连接已关闭")
        data.extend(chunk)
        if len(data) > limit:
            raise OSError("请求头超过上限")
    head, rest = bytes(data).split(b"\r\n\r\n", 1)
    return head, rest


def _read_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        buf = sock.recv(count - len(data))
        if not buf:
            raise OSError("上游代理提前断开")
        data.extend(buf)
    return bytes(data)


def _socks_connect(sock: socket.socket, spec: ProxySpec, host: str, port: int):
    authenticated = bool(spec.username)
    method = b"\x02" if authenticated else b"\x00"
    sock.sendall(b"\x05\x01" + method)
    if _read_exact(sock, 2) != b"\x05" + method:
        raise OSError("SOCKS5 代理不接受指定认证方式")
    if authenticated:
        user, passwd = spec.username.encode(), spec.password.encode()
        sock.sendall(bytes((1, len(user))) + user + bytes((len(passwd),)) + passwd)
        if _read_exact(sock, 2) != b"\x01\x00":
            raise OSError("SOCKS5 认证失败")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        addr = host.encode("idna")
        if len(addr) > 255:
            raise OSError("目标域名过长")
        target = bytes((3, len(addr))) + addr
    else:
        target = bytes((1 if ip.version == 4 else 4,)) + ip.packed
    sock.sendall(b"\x05\x01\x00" + target + port.to_bytes(2, "big"))
    code = _read_exact(sock, 4)
    if code[:2] != b"\x05\x00":
        raise OSError("SOCKS5 上游拒绝连接")
    length = {1: 4, 4: 16}.get(code[3])
    if code[3] == 3:
        length = _read_exact(sock, 1)[0]
    if length is None:
        raise OSError("SOCKS5 返回非法地址类型")
    _read_exact(sock, length + 2)


def _tunnel(left: socket.socket, right: socket.socket):
    sockets = (left, right)
    for s in sockets:
        s.settimeout(None)
    while True:
        readable, _, _ = select.select(sockets, [], [], 600)
        if not readable:
            break
        for src in readable:
            data = src.recv(65536)
            if not data:
                return
            (right if src is left else left).sendall(data)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(30)
        upstream = None
        replied = False
        try:
            head, extra = _read_headers(client)
            lines = head.split(b"\r\n")
            parts = lines[0].decode("ascii").split(" ")
            if len(parts) != 3:
                raise OSError("代理请求行无效")
            method, target, version = parts
            if not version.startswith("HTTP/1."):
                raise OSError("不支持的 HTTP 版本")
            if method.upper() == "CONNECT":
                if ":" not in target:
                    raise OSError("CONNECT 缺少端口")
                host, port_s = target.rsplit(":", 1)
                host = host.strip("[]")
                port = int(port_s)
            else:
                target_url = urlsplit(target)
                if target_url.scheme.lower() != "http" or not target_url.hostname:
                    raise OSError("只支持 HTTP 代理请求和 CONNECT 隧道")
                host, port = target_url.hostname, target_url.port or 80
            if not host or not 1 <= port <= 65535:
                raise OSError("非法目标地址")

            spec = self.server.spec
            upstream = socket.create_connection((spec.host, spec.port), timeout=15)
            upstream.settimeout(30)
            if spec.scheme == "socks5":
                _socks_connect(upstream, spec, host, port)
                if method.upper() == "CONNECT":
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    replied = True
                    if extra:
                        upstream.sendall(extra)
                    _tunnel(client, upstream)
                else:
                    origin = target_url.path or "/"
                    if target_url.query:
                        origin += "?" + target_url.query
                    filtered = [ln for ln in lines[1:]
                                if ln.split(b":", 1)[0].lower() not in
                                (b"proxy-authorization", b"proxy-connection")]
                    upstream.sendall((f"{method} {origin} {version}\r\n".encode("ascii")
                                      + b"\r\n".join(filtered) + b"\r\nConnection: close\r\n\r\n" + extra))
                    replied = True
                    _tunnel(client, upstream)
            else:
                auth_line = b""
                if spec.username:
                    auth = base64.b64encode(f"{spec.username}:{spec.password}".encode()).decode("ascii")
                    auth_line = f"Proxy-Authorization: Basic {auth}\r\n".encode("ascii")
                if method.upper() == "CONNECT":
                    upstream.sendall((f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode("ascii")
                                      + auth_line + b"Proxy-Connection: close\r\n\r\n"))
                    response, trailing = _read_headers(upstream)
                    status = response.split(b"\r\n", 1)[0].split(b" ")
                    if len(status) < 2 or status[1] != b"200":
                        raise OSError("上游 HTTP 代理连接或认证失败")
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    replied = True
                    if extra:
                        upstream.sendall(extra)
                    if trailing:
                        client.sendall(trailing)
                    _tunnel(client, upstream)
                else:
                    filtered = [ln for ln in lines[1:]
                                if ln.split(b":", 1)[0].lower() not in
                                (b"proxy-authorization", b"proxy-connection")]
                    upstream.sendall((lines[0] + b"\r\n" + b"\r\n".join(filtered)
                                      + b"\r\n" + auth_line + b"Connection: close\r\n\r\n" + extra))
                    replied = True
                    _tunnel(client, upstream)
        except (OSError, ValueError, UnicodeError):
            if not replied:
                try:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass
        finally:
            if upstream:
                upstream.close()


class ProxyBridgeManager:
    """每个账号固定一个 loopback 监听端口；修改配置时关闭旧端口。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries = {}

    def local_url(self, account_id: str, proxy_url: str) -> str:
        spec = parse_proxy(proxy_url)
        if not account_id:
            raise ProxyConfigError("缺少账号 ID，拒绝创建认证代理通道")
        with self._lock:
            entry = self._entries.get(account_id)
            if entry and entry[0] == proxy_url:
                return f"http://127.0.0.1:{entry[1].server_address[1]}"
            if entry:
                entry[1].shutdown()
                entry[1].server_close()
                del self._entries[account_id]
            try:
                server = _Server(("127.0.0.1", 0), _Handler)
                server.spec = spec
                worker = threading.Thread(target=server.serve_forever, daemon=True,
                                          name=f"muse-proxy-{account_id[:8]}")
                worker.start()
            except OSError as exc:
                raise ProxyConfigError("本地代理转发器启动失败") from None
            self._entries[account_id] = (proxy_url, server, worker)
            return f"http://127.0.0.1:{server.server_address[1]}"

    def close(self, account_id: str):
        with self._lock:
            entry = self._entries.pop(account_id, None)
            if entry:
                entry[1].shutdown()
                entry[1].server_close()

    def close_all(self):
        with self._lock:
            ids = list(self._entries)
        for account_id in ids:
            self.close(account_id)
